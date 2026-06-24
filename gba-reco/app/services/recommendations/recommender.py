"""V3.2 hybrid recommender — clean port.

Repurchase (segment-weighted frequency×recency) + Discovery (Jaccard collaborative)
+ strict 20/5 mix + group diversity. Parameterized SQL, typed, config-driven.
Carried from bi-server-concord prototype; hardened and de-magic-numbered.
"""
from __future__ import annotations

import math
from datetime import datetime

from app.core.config import get_settings
from app.data import cache
from app.data import sales_repository as repo
from app.domain.models import ProductRec, RecommendationResult, RecSource, Segment

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
    return _recency_scores_from_last_purchase(last, as_of_date)


def _recency_scores_from_last_purchase(last: dict[int, object], as_of_date: str) -> dict[int, float]:
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
    sims.sort(key=lambda x: x[1], reverse=True)
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
) -> list[ProductRec]:
    """Fill the gap to top_n when V3.2 discovery under-delivers (HEAVY/LIGHT clients with weak
    Jaccard neighbourhoods). Source order: co-purchase item-CF discovery, then ubiquity-filtered
    global popularity. Everything is filtered against the rec exclusion set, negatives, and the ids
    already in `combined`, so synthetic/ubiquitous lines and dupes never leak in.

    Backfilled items are appended below the existing ranking with monotonically decreasing scores,
    preserving the primary V3.2 ordering."""
    from app.services.eval import baselines
    from app.services.recommendations import copurchase

    if len(combined) >= top_n:
        return combined
    blocked = set(excl) | set(cache.get_negatives(customer_id)) | {r.product_id for r in combined}
    base_score = min((r.score for r in combined), default=0.0)
    step = 1e-4
    rank_offset = 1

    def _emit(pid: int) -> ProductRec:
        nonlocal rank_offset
        rec = ProductRec(
            product_id=pid, score=round(base_score - step * rank_offset, 6),
            rank=len(combined) + rank_offset, segment=segment.value, source=RecSource.DISCOVERY,
        )
        rank_offset += 1
        return rec

    try:
        cop = copurchase.recommend(customer_id, as_of, top_n=top_n * 2, include_owned=False)
        for r in cop.recommendations:
            if len(combined) >= top_n:
                break
            if r.product_id in blocked:
                continue
            blocked.add(r.product_id)
            combined.append(_emit(r.product_id))
    except Exception:  # noqa: BLE001
        pass

    if len(combined) < top_n:
        need = top_n - len(combined)
        for pid in baselines.global_popular(as_of, need + len(blocked), exclude=excl):
            if len(combined) >= top_n:
                break
            if pid in blocked:
                continue
            blocked.add(pid)
            combined.append(_emit(pid))

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
    top_n = top_n or s.default_top_n
    repurchase_n = min(s.repurchase_count, top_n)
    discovery_n = max(top_n - repurchase_n, 0) if include_discovery else 0

    segment = classify(customer_id, as_of)
    w_freq, w_rec = _WEIGHTS[segment]

    # byRegion scoping (opt-in): restrict the discovery neighbour pool to the client's oblast.
    # Repurchase is the client's OWN history and is region-invariant, so only discovery is scoped.
    # Fail-open when the client has no region set.
    region_id = repo.client_region_id(customer_id) if region_scope else None

    excl = repo.ubiquitous_product_ids(s.ubiquity_exclude_pct)
    purchase_stats = repo.product_purchase_stats(customer_id, as_of)
    freq = _normalize({pid: float(cnt) for pid, (cnt, _dt) in purchase_stats.items() if pid not in excl})
    rec = _normalize({
        pid: v
        for pid, v in _recency_scores_from_last_purchase(
            {pid: dt for pid, (_cnt, dt) in purchase_stats.items()},
            as_of,
        ).items()
        if pid not in excl
    })
    owned = set(freq) | set(rec)

    repurchase_scores = {pid: w_freq * freq.get(pid, 0.0) + w_rec * rec.get(pid, 0.0) for pid in owned}
    ranked = sorted(repurchase_scores.items(), key=lambda x: x[1], reverse=True)

    # over-fetch to survive diversity filtering
    repurchase = [
        ProductRec(product_id=pid, score=float(sc), rank=i + 1, segment=segment.value,
                   source=RecSource.REPURCHASE)
        for i, (pid, sc) in enumerate(ranked[: repurchase_n + 10])
    ]
    repurchase = _diversity_filter(repurchase, s.max_per_group)[:repurchase_n]

    discovery: list[ProductRec] = []
    if discovery_n > 0:
        sims = _similar_customers(customer_id, as_of, region_id=region_id)
        collab = {pid: v for pid, v in repo.collaborative_products(sims, owned, as_of).items()
                  if pid not in excl}
        d_ranked = sorted(collab.items(), key=lambda x: x[1], reverse=True)
        discovery = [
            ProductRec(product_id=pid, score=float(sc), rank=i + 1, segment=segment.value,
                       source=RecSource.DISCOVERY)
            for i, (pid, sc) in enumerate(d_ranked[: discovery_n + 5])
        ]
        discovery = _diversity_filter(discovery, s.max_per_group)[:discovery_n]

    combined = repurchase + discovery
    if include_discovery and len(combined) < top_n:
        combined = _backfill(combined, customer_id, as_of, top_n, segment, excl)
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
    )
