"""Co-purchase item-item recommender — a stronger candidate than V3.2's client-Jaccard.

Idea: products bought together / by the same clients are related. For a client we score
candidate products by similarity to what the client already buys, weighted by the client's
own affinity (frequency). This is classic item-item collaborative filtering, which usually
beats user-user CF on sparse B2B catalogs and is cheap to compute.

Similarity: cosine over the client×product co-occurrence (a product is a vector of which
clients bought it). Computed point-in-time (source_rules.window) so it's eval-safe.

No heavy deps — pure SQL aggregation + dict math. Designed to be A/B'd against V3.2 and the
naive baselines through the SAME eval harness (returns ranked product_ids).
"""
from __future__ import annotations

import math
from collections import defaultdict

from app.core.config import get_settings
from app.core.history import full_history_coverage
from app.data import cache
from app.data import sales_repository as repo
from app.data import source_rules as rules
from app.data.db import in_clause, query
from app.domain.models import (
    ProductRec,
    RecommendationResult,
    RecSource,
    RecSourceDetail,
)
from app.services.eval import measured

_COOC_ROW_CAP = 1500


def _client_products_with_freq(
    customer_id: int, as_of: str, client_agreement_net_id: str | None = None,
) -> dict[int, float]:
    rows = query(
        f"""
        SELECT oi.ProductID AS pid, COUNT(DISTINCT o.ID) AS c
        FROM {rules.SPINE}
        WHERE ca.ClientID = :cid
              AND (:agreement_uid IS NULL OR ca.NetUID = CONVERT(uniqueidentifier, :agreement_uid))
              AND {rules.PURCHASED}
        GROUP BY oi.ProductID
        ORDER BY COUNT(DISTINCT o.ID) DESC, MAX({rules.SALE_INSTANT}) DESC, oi.ProductID ASC
        """,
        {
            "cid": customer_id,
            "agreement_uid": client_agreement_net_id,
            **rules.window(as_of),
        },
    )
    return {int(r["pid"]): float(r["c"]) for r in rows}


def _cooccurring_products(seed_products: list[int], as_of: str, limit_seed: int = 50) -> dict[int, float]:
    """For the client's seed products, find products co-bought by the same clients,
    scored by cosine-style similarity. Single aggregated query (no N+1).

    sim(seed, cand) ~ co_clients / sqrt(deg(seed) * deg(cand)), summed over seeds.
    """
    if not seed_products:
        return {}
    seeds = seed_products[:limit_seed]
    seed_ph, seed_params = in_clause("s", seeds)

    # degree (distinct clients) per product, restricted to relevant products for speed
    # candidates = products co-bought by clients who bought a seed product
    rows = query(
        f"""
        WITH seed_clients AS (
            SELECT DISTINCT ca.ClientID AS cid, oi.ProductID AS seed
            FROM {rules.SPINE}
            WHERE {rules.PURCHASED}
                  AND oi.ProductID IN {seed_ph}
        ),
        cand AS (
            SELECT DISTINCT ca.ClientID AS cid, oi.ProductID AS cand
            FROM {rules.SPINE}
            WHERE {rules.PURCHASED}
                  AND ca.ClientID IN (SELECT sc.cid FROM seed_clients sc)
        )
        SELECT TOP (:cap) sc.seed AS seed, c.cand AS cand, COUNT(DISTINCT sc.cid) AS co_clients
        FROM seed_clients sc
        JOIN cand c ON c.cid = sc.cid
        GROUP BY sc.seed, c.cand
        HAVING COUNT(DISTINCT sc.cid) >= 2
        ORDER BY COUNT(DISTINCT sc.cid) DESC, sc.seed ASC, c.cand ASC
        """,
        {
            "cap": _COOC_ROW_CAP,
            **rules.window(as_of),
            **seed_params,
        },
    )
    if not rows:
        return {}

    # degree per product (distinct clients) for the products we touched
    touched = {int(r["seed"]) for r in rows} | {int(r["cand"]) for r in rows}
    deg = _product_degrees(list(touched), as_of)

    scores: dict[int, float] = defaultdict(float)
    for r in rows:
        seed = int(r["seed"])
        cand = int(r["cand"])
        co = float(r["co_clients"])
        denom = math.sqrt(max(deg.get(seed, 1), 1) * max(deg.get(cand, 1), 1))
        scores[cand] += co / denom
    return dict(scores)


def _product_degrees(product_ids: list[int], as_of: str) -> dict[int, int]:
    if not product_ids:
        return {}
    ph, params = in_clause("p", product_ids)
    rows = query(
        f"""
        SELECT oi.ProductID AS pid, COUNT(DISTINCT ca.ClientID) AS deg
        FROM {rules.SPINE}
        WHERE {rules.PURCHASED}
              AND oi.ProductID IN {ph}
        GROUP BY oi.ProductID
        """,
        {
            **rules.window(as_of),
            **params,
        },
    )
    return {int(r["pid"]): int(r["deg"]) for r in rows}


def recommend(
    customer_id: int,
    as_of_date: str,
    top_n: int = 25,
    include_owned: bool = True,
    seed_product_ids: list[int] | None = None,
    client_agreement_net_id: str | None = None,
) -> RecommendationResult:
    """Hybrid: repurchase (client's own freq) blended with co-purchase item-CF for discovery.

    With `seed_product_ids` the co-occurrence is seeded by those explicit products (the sale
    wizard's per-cart-line cross-sell: «з ЦИМ товаром зазвичай беруть ще й це») instead of the
    client's own purchase history — so it works even for clients with no valid sales yet."""
    from datetime import datetime
    scope = {"client_agreement_net_id": client_agreement_net_id} if client_agreement_net_id else {}
    started = datetime.now()
    history = full_history_coverage(as_of_date)

    excl = (repo.ubiquitous_product_ids(get_settings().ubiquity_exclude_pct, as_of_date)
            | cache.get_negatives(customer_id)
            | repo.open_order_product_ids(customer_id, as_of_date))
    owned_live = repo.owned_live_product_ids(customer_id, as_of_date)
    own = {
        p: f for p, f in _client_products_with_freq(customer_id, as_of_date, **scope).items()
        if p not in excl
    }
    seeds = [p for p in seed_product_ids if p not in excl] if seed_product_ids else list(own.keys())
    co = {p: s for p, s in _cooccurring_products(seeds, as_of_date).items() if p not in excl}

    # normalize each signal
    def _norm(d: dict[int, float]) -> dict[int, float]:
        if not d:
            return {}
        top = max(d.values()) or 1.0
        return {k: v / top for k, v in d.items()}

    own_n = _norm(own)
    co_n = _norm(co)

    combined: dict[int, tuple[float, RecSource]] = {}
    for pid, s in own_n.items():
        combined[pid] = (0.6 * s, RecSource.REPURCHASE)
    for pid, s in co_n.items():
        prev = combined.get(pid, (0.0, RecSource.DISCOVERY))
        # co-purchase boosts both owned (reinforce) and new (discovery)
        src = RecSource.REPURCHASE if pid in own_n else RecSource.DISCOVERY
        combined[pid] = (prev[0] + 0.4 * s, src)

    ranked = sorted(combined.items(), key=lambda item: (-item[1][0], item[0]))
    if not include_owned:
        # discovery-only (new-to-client co-purchase items) — the cross-sell use case
        ranked = [item for item in ranked if item[1][1] == RecSource.DISCOVERY]
    if seed_product_ids:
        # never recommend the seed itself (it is already in the caller's cart)
        seed_set = set(seed_product_ids)
        ranked = [item for item in ranked if item[0] not in seed_set]
    # Every result must be actionable, including repurchase. One set-membership query over a
    # bounded top-of-ranking window checks the canonical operational resale stock.
    window = ranked[: max(top_n * 4, 100)]
    stocked = repo.in_stock_product_ids([pid for pid, _ in window], **scope)
    ranked = [item for item in window if item[0] in stocked][:top_n]
    recs = [
        ProductRec(product_id=pid, score=round(score, 4), rank=i + 1,
                   segment="COPURCHASE", source=src,
                   source_detail=(
                       RecSourceDetail.REPURCHASE_HISTORY
                       if src == RecSource.REPURCHASE
                       else RecSourceDetail.COPURCHASE
                   ))
        for i, (pid, (score, src)) in enumerate(ranked)
    ]
    # Translate dead catalog-generation ids onto live rows (see live_remap docstring).
    from app.services.recommendations import live_remap
    recs = live_remap.remap_recs_to_live(recs)
    for item in recs:
        if item.product_id in owned_live:
            item.source = RecSource.REPURCHASE
            item.source_detail = RecSourceDetail.REPURCHASE_HISTORY
    if not include_owned:
        # The first filter above compares historical raw ids. Repeat after live remapping so a
        # different catalog generation of an already-bought product cannot pass as discovery.
        recs = [item for item in recs if item.source == RecSource.DISCOVERY]
    final_stock = repo.in_stock_product_ids([item.product_id for item in recs], **scope)
    recs = [item for item in recs if item.product_id in final_stock]
    for index, item in enumerate(recs, start=1):
        item.rank = index
    discovery = sum(1 for r in recs if r.source == RecSource.DISCOVERY)
    latency = (datetime.now() - started).total_seconds() * 1000
    return RecommendationResult(
        customer_id=customer_id, client_agreement_net_id=client_agreement_net_id,
        recommendations=recs, count=len(recs),
        discovery_count=discovery, segment="COPURCHASE",
        precision_estimate=measured.precision("copurchase", top_n),
        latency_ms=round(latency, 2), as_of_date=as_of_date,
        source_history_start=history.source_history_start.isoformat(),
        effective_start=history.effective_start.isoformat(),
        history_complete=history.history_complete,
        model_version="copurchase-v5-confirmed-sales-history-floor-20250101",
    )
