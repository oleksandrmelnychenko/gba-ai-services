"""Co-purchase item-item recommender — a stronger candidate than V3.2's client-Jaccard.

Idea: products bought together / by the same clients are related. For a client we score
candidate products by similarity to what the client already buys, weighted by the client's
own affinity (frequency). This is classic item-item collaborative filtering, which usually
beats user-user CF on sparse B2B catalogs and is cheap to compute.

Similarity: cosine over the client×product co-occurrence (a product is a vector of which
clients bought it). Computed point-in-time (Created < as_of) so it's eval-safe.

No heavy deps — pure SQL aggregation + dict math. Designed to be A/B'd against V3.2 and the
naive baselines through the SAME eval harness (returns ranked product_ids).
"""
from __future__ import annotations

import math
from collections import defaultdict

from app.core.config import get_settings
from app.data import cache
from app.data import sales_repository as repo
from app.data.db import in_clause, query
from app.domain.models import ProductRec, RecommendationResult, RecSource

_COOC_ROW_CAP = 1500


def _client_products_with_freq(customer_id: int, as_of: str) -> dict[int, float]:
    rows = query(
        """
        SELECT oi.ProductID AS pid, COUNT(DISTINCT o.ID) AS c
        FROM dbo.ClientAgreement ca
        JOIN dbo.[Order] o ON ca.ID = o.ClientAgreementID
        JOIN dbo.OrderItem oi ON oi.OrderID = o.ID
        WHERE ca.ClientID = :cid AND oi.IsValidForCurrentSale = 1
              AND o.Created < :asof AND oi.ProductID IS NOT NULL
        GROUP BY oi.ProductID
        """,
        {"cid": customer_id, "asof": as_of},
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
            FROM dbo.ClientAgreement ca
            JOIN dbo.[Order] o ON ca.ID = o.ClientAgreementID
            JOIN dbo.OrderItem oi ON oi.OrderID = o.ID
            WHERE oi.IsValidForCurrentSale = 1 AND o.Created < :asof AND oi.ProductID IN {seed_ph}
        ),
        cand AS (
            SELECT DISTINCT ca.ClientID AS cid, oi.ProductID AS cand
            FROM seed_clients sc
            JOIN dbo.ClientAgreement ca ON ca.ClientID = sc.cid
            JOIN dbo.[Order] o ON ca.ID = o.ClientAgreementID
            JOIN dbo.OrderItem oi ON oi.OrderID = o.ID
            WHERE oi.IsValidForCurrentSale = 1 AND o.Created < :asof AND oi.ProductID IS NOT NULL
        )
        SELECT TOP (:cap) sc.seed AS seed, c.cand AS cand, COUNT(DISTINCT sc.cid) AS co_clients
        FROM seed_clients sc
        JOIN cand c ON c.cid = sc.cid
        GROUP BY sc.seed, c.cand
        HAVING COUNT(DISTINCT sc.cid) >= 2
        ORDER BY COUNT(DISTINCT sc.cid) DESC
        """,
        {"asof": as_of, "cap": _COOC_ROW_CAP, **seed_params},
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
        FROM dbo.ClientAgreement ca
        JOIN dbo.[Order] o ON ca.ID = o.ClientAgreementID
        JOIN dbo.OrderItem oi ON oi.OrderID = o.ID
        WHERE oi.IsValidForCurrentSale = 1 AND o.Created < :asof AND oi.ProductID IN {ph}
        GROUP BY oi.ProductID
        """,
        {"asof": as_of, **params},
    )
    return {int(r["pid"]): int(r["deg"]) for r in rows}


def recommend(
    customer_id: int,
    as_of_date: str,
    top_n: int = 25,
    include_owned: bool = True,
) -> RecommendationResult:
    """Hybrid: repurchase (client's own freq) blended with co-purchase item-CF for discovery."""
    from datetime import datetime
    started = datetime.now()

    excl = repo.ubiquitous_product_ids(get_settings().ubiquity_exclude_pct) | cache.get_negatives(customer_id)
    own = {p: f for p, f in _client_products_with_freq(customer_id, as_of_date).items() if p not in excl}
    co = {p: s for p, s in _cooccurring_products(list(own.keys()), as_of_date).items() if p not in excl}

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

    ranked = sorted(combined.items(), key=lambda x: x[1][0], reverse=True)
    if not include_owned:
        # discovery-only (new-to-client co-purchase items) — the cross-sell use case
        ranked = [item for item in ranked if item[1][1] == RecSource.DISCOVERY]
    ranked = ranked[:top_n]
    recs = [
        ProductRec(product_id=pid, score=round(score, 4), rank=i + 1,
                   segment="COPURCHASE", source=src)
        for i, (pid, (score, src)) in enumerate(ranked)
    ]
    discovery = sum(1 for r in recs if r.source == RecSource.DISCOVERY)
    latency = (datetime.now() - started).total_seconds() * 1000
    return RecommendationResult(
        customer_id=customer_id, recommendations=recs, count=len(recs),
        discovery_count=discovery, segment="COPURCHASE",
        latency_ms=round(latency, 2), as_of_date=as_of_date, model_version="copurchase-v1",
    )
