"""V3.2 hybrid recommender — clean port.

Repurchase (segment-weighted frequency×recency) + Discovery (Jaccard collaborative)
+ strict 20/5 mix + group diversity. Parameterized SQL, typed, config-driven.
Carried from bi-server-concord prototype; hardened and de-magic-numbered.
"""
from __future__ import annotations

import math
from datetime import datetime

from app.core.config import get_settings
from app.core.history import full_history_coverage
from app.core.logging import get_logger
from app.data import cache
from app.data import sales_repository as repo
from app.domain.models import (
    ProductRec,
    RecommendationResult,
    RecSource,
    RecSourceDetail,
    Segment,
)
from app.services.recommendations import live_remap

log = get_logger("recommender")

# Segment-specific repurchase weights (frequency, recency). Re-tuned on the leave-last-basket
# harness (n=493) after the recency-scale fix put freq and recency on the same [0,1] scale.
_WEIGHTS: dict[Segment, tuple[float, float]] = {
    Segment.HEAVY: (0.40, 0.60),
    Segment.REGULAR_CONSISTENT: (0.40, 0.60),
    Segment.REGULAR_EXPLORATORY: (0.30, 0.70),
    Segment.LIGHT: (0.30, 0.70),
}

_RECENCY_HALFLIFE_DAYS = 21
_MIN_SIMILARITY = 0.05
_MAX_SIMILAR = 100
# Top-of-ranking window sent to the single in_stock_product_ids set-membership query — bounds
# the IN-clause parameter count while leaving ample slack over discovery_n after filtering.
_STOCK_POOL_CAP = 1000


def classify(customer_id: int, as_of_date: str) -> Segment:
    orders = repo.count_orders_before(customer_id, as_of_date)
    if orders >= 500:
        return Segment.HEAVY
    if orders >= 100:
        rate = repo.repurchase_rate(customer_id, as_of_date)
        return Segment.REGULAR_CONSISTENT if rate >= 0.40 else Segment.REGULAR_EXPLORATORY
    return Segment.LIGHT


def _normalize(scores: dict[int, float]) -> dict[int, float]:
    if not scores:
        return {}
    top = max(scores.values()) or 1.0
    return {k: v / top for k, v in scores.items()}


def _recency_scores(customer_id: int, as_of_date: str) -> dict[int, float]:
    last = repo.product_last_purchase(customer_id, as_of_date)
    asof = datetime.fromisoformat(as_of_date)
    out: dict[int, float] = {}
    for pid, dt in last.items():
        if dt is None:
            continue
        days = (asof - dt).days
        out[pid] = math.exp(-days / _RECENCY_HALFLIFE_DAYS)
    return out


def _similar_customers(customer_id: int, as_of_date: str,
                       region_id: int | None = None) -> list[tuple[int, float]]:
    target = repo.customer_products(customer_id, as_of_date)
    if not target:
        return []
    candidates = repo.candidate_similar_customers(target, customer_id, as_of_date,
                                                  region_id=region_id)
    others = repo.customer_products_bulk(candidates, as_of_date)  # one query, not N
    sims: list[tuple[int, float]] = []
    for cid in candidates:
        other = others.get(cid)
        if not other:
            continue
        union = len(target | other)
        if union == 0:
            continue
        jac = len(target & other) / union
        if jac >= _MIN_SIMILARITY:
            sims.append((cid, jac))
    sims.sort(key=lambda item: (-item[1], item[0]))
    return sims[:_MAX_SIMILAR]


def _diversity_filter(recs: list[ProductRec], max_per_group: int) -> list[ProductRec]:
    groups = repo.product_groups([r.product_id for r in recs])
    counts: dict[int, int] = {}
    kept: list[ProductRec] = []
    for r in recs:
        gid = groups.get(r.product_id)
        if gid is None or counts.get(gid, 0) < max_per_group:
            kept.append(r)
            if gid is not None:
                counts[gid] = counts.get(gid, 0) + 1
    return kept


def _backfill(
    combined: list[ProductRec],
    customer_id: int,
    as_of: str,
    top_n: int,
    segment: Segment,
    excl: frozenset[int],
    owned_live: frozenset[int],
) -> list[ProductRec]:
    """Fill the gap to top_n when V3.2 discovery under-delivers (HEAVY/LIGHT clients with weak
    Jaccard neighbourhoods). Source order: co-purchase item-CF discovery, then ubiquity-filtered
    global popularity. Everything is filtered against the rec exclusion set (which folds in the
    client's negative-feedback set) and the ids already in `combined`, so synthetic/ubiquitous
    lines, negatived products and dupes never leak in. Zero-stock
    candidates are dropped: copurchase filters its discovery output internally; the popularity
    pool is checked here with one in_stock_product_ids set-membership query. Candidates are
    also compared to the client's complete purchase history after both sides are resolved onto
    live catalog identities, so a re-minted generation of an owned product cannot be mislabeled
    as discovery.

    Backfilled items are appended below the existing ranking with monotonically decreasing scores,
    preserving the primary V3.2 ordering."""
    from app.services.eval import baselines
    from app.services.recommendations import copurchase

    if len(combined) >= top_n:
        return combined
    blocked = set(excl) | {r.product_id for r in combined}
    base_score = min((r.score for r in combined), default=1.0)
    step = 1e-4
    emitted_count = 0

    def _emit(pid: int, source_detail: RecSourceDetail) -> ProductRec:
        nonlocal emitted_count
        emitted_count += 1
        rec = ProductRec(
            product_id=pid, score=round(max(base_score - step * emitted_count, 0.0), 6),
            rank=len(combined) + 1, segment=segment.value, source=RecSource.DISCOVERY,
            source_detail=source_detail,
        )
        return rec

    try:
        cop = copurchase.recommend(customer_id, as_of, top_n=top_n * 2, include_owned=False)
        for r in cop.recommendations:
            if len(combined) >= top_n:
                break
            if r.product_id in blocked or r.product_id in owned_live:
                continue
            blocked.add(r.product_id)
            combined.append(_emit(r.product_id, RecSourceDetail.COPURCHASE))
    except Exception:  # noqa: BLE001
        pass

    if len(combined) < top_n:
        pool_size = min(_STOCK_POOL_CAP, max(top_n * 5, top_n + len(blocked)))
        popular = baselines.global_popular(as_of, pool_size, exclude=excl)
        popular_live = live_remap.live_product_map(popular)
        stocked = repo.in_stock_product_ids(popular)
        for pid in popular:
            if len(combined) >= top_n:
                break
            live_id = popular_live.get(pid)
            if (
                pid in blocked
                or pid not in stocked
                or live_id is None
                or live_id in owned_live
            ):
                continue
            blocked.add(pid)
            combined.append(_emit(pid, RecSourceDetail.GLOBAL_POPULAR))

    return combined


def recommend(
    customer_id: int,
    as_of_date: str | None = None,
    top_n: int | None = None,
    include_discovery: bool = True,
    region_scope: bool = False,
) -> RecommendationResult:
    s = get_settings()
    started = datetime.now()
    as_of = as_of_date or datetime.now().strftime("%Y-%m-%d")
    history = full_history_coverage(as_of)
    top_n = top_n or s.default_top_n
    repurchase_n = min(s.repurchase_count, top_n)
    discovery_n = max(top_n - repurchase_n, 0) if include_discovery else 0

    segment = classify(customer_id, as_of)
    w_freq, w_rec = _WEIGHTS[segment]

    # byRegion scoping (opt-in): restrict the discovery neighbour pool to the client's oblast.
    # Repurchase is the client's OWN history and is region-invariant, so only discovery is scoped.
    # Fail-open when the client has no region set.
    region_id = repo.client_region_id(customer_id) if region_scope else None

    excl = repo.ubiquitous_product_ids(s.ubiquity_exclude_pct) | cache.get_negatives(customer_id)
    owned_live = (
        frozenset(repo.owned_live_product_ids(customer_id, as_of))
        if include_discovery
        else frozenset()
    )
    freq = _normalize({pid: float(c) for pid, c in repo.product_frequency(customer_id, as_of).items()
                       if pid not in excl})
    rec = _normalize({pid: v for pid, v in _recency_scores(customer_id, as_of).items()
                      if pid not in excl})
    owned = set(freq) | set(rec)

    repurchase_scores = {pid: w_freq * freq.get(pid, 0.0) + w_rec * rec.get(pid, 0.0) for pid in owned}
    ranked = sorted(repurchase_scores.items(), key=lambda item: (-item[1], item[0]))

    # Repurchase must be actionable too: over-fetch, then require the same operational resale
    # stock used for discovery before diversity filtering.
    repurchase_pool = ranked[:_STOCK_POOL_CAP]
    stocked_repurchase = repo.in_stock_product_ids([pid for pid, _ in repurchase_pool])
    repurchase_pool = [
        (pid, score) for pid, score in repurchase_pool if pid in stocked_repurchase
    ]
    repurchase = [
        ProductRec(product_id=pid, score=float(sc), rank=i + 1, segment=segment.value,
                   source=RecSource.REPURCHASE,
                   source_detail=RecSourceDetail.REPURCHASE_HISTORY)
        for i, (pid, sc) in enumerate(repurchase_pool[: repurchase_n + 10])
    ]
    repurchase = _diversity_filter(repurchase, s.max_per_group)[:repurchase_n]

    discovery: list[ProductRec] = []
    if discovery_n > 0:
        try:
            sims = _similar_customers(customer_id, as_of, region_id=region_id)
            collab = {pid: v for pid, v in repo.collaborative_products(sims, as_of, customer_id).items()
                      if pid not in excl}
            d_ranked = sorted(
                collab.items(), key=lambda item: (-item[1], item[0])
            )[:_STOCK_POOL_CAP]
            candidate_live = live_remap.live_product_map([pid for pid, _ in d_ranked])
            d_ranked = [
                (pid, score)
                for pid, score in d_ranked
                if candidate_live.get(pid) not in owned_live
            ]
            stocked = repo.in_stock_product_ids([pid for pid, _ in d_ranked])
            d_ranked = [(pid, sc) for pid, sc in d_ranked if pid in stocked]
            discovery = [
                ProductRec(product_id=pid, score=float(sc), rank=i + 1, segment=segment.value,
                           source=RecSource.DISCOVERY,
                           source_detail=RecSourceDetail.SIMILAR_CLIENTS)
                for i, (pid, sc) in enumerate(d_ranked[: discovery_n + 5])
            ]
            discovery = _diversity_filter(discovery, s.max_per_group)[:discovery_n]
        except Exception as exc:  # noqa: BLE001
            log.warning("discovery_degraded", customer_id=customer_id, error=str(exc))
            discovery = []

    combined = repurchase + discovery
    if include_discovery and len(combined) < top_n:
        combined = _backfill(
            combined,
            customer_id,
            as_of,
            top_n,
            segment,
            excl,
            owned_live,
        )
    # History carries product ids of soft-deleted catalog generations (re-syncs mint new
    # rows); translate onto live rows so the gba-server hydration doesn't drop the list.
    combined = live_remap.remap_recs_to_live(combined)
    mislabeled_owned = [
        item.product_id
        for item in combined
        if item.source == RecSource.DISCOVERY and item.product_id in owned_live
    ]
    if mislabeled_owned:
        log.error(
            "owned_products_blocked_from_discovery",
            customer_id=customer_id,
            product_ids=mislabeled_owned,
        )
        combined = [
            item
            for item in combined
            if item.source != RecSource.DISCOVERY or item.product_id not in owned_live
        ]
    # Defense in depth: the exact live ids returned to the caller must still be in operational
    # resale stock. The repository uses the same remap rule, so this should remove nothing.
    final_stock = repo.in_stock_product_ids([item.product_id for item in combined])
    combined = [item for item in combined if item.product_id in final_stock]
    for i, r in enumerate(combined):
        r.rank = i + 1

    discovery_count = sum(1 for r in combined if r.source == RecSource.DISCOVERY)
    latency_ms = (datetime.now() - started).total_seconds() * 1000
    return RecommendationResult(
        customer_id=customer_id,
        recommendations=combined,
        count=len(combined),
        discovery_count=discovery_count,
        segment=segment.value,
        latency_ms=round(latency_ms, 2),
        cached=False,
        as_of_date=as_of,
        source_history_start=history.source_history_start.isoformat(),
        effective_start=history.effective_start.isoformat(),
        history_complete=history.history_complete,
    )
