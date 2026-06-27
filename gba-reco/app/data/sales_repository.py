"""Parameterized read queries over the sales spine (ClientAgreement -> Order -> OrderItem).

All SQL here is parameterized (:name) — no f-string interpolation (prototype anti-pattern).
as_of_date enables point-in-time recommendations for time-split evaluation.
"""
from __future__ import annotations

import threading
import time

from app.core.config import get_settings
from app.data.db import in_clause, query

_UBIQUITY_CACHE: dict[float, tuple[float, frozenset[int]]] = {}
_UBIQUITY_LOCK = threading.Lock()


def _query_ubiquitous(pct: float) -> frozenset[int]:
    rows = query(
        """
        WITH base AS (
            SELECT ca.ClientID AS cid, oi.ProductID AS pid
            FROM dbo.[Order] o
            JOIN dbo.ClientAgreement ca ON ca.ID = o.ClientAgreementID
            JOIN dbo.OrderItem oi ON oi.OrderID = o.ID
            WHERE oi.IsValidForCurrentSale = 1 AND oi.ProductID IS NOT NULL
                  AND o.Created >= DATEADD(month, -12, GETDATE())
        ),
        tot AS (SELECT COUNT(DISTINCT cid) AS n FROM base)
        SELECT b.pid AS pid
        FROM base b CROSS JOIN tot
        GROUP BY b.pid, tot.n
        HAVING COUNT(DISTINCT b.cid) * 1.0 / NULLIF(tot.n, 0) > :pct
        """,
        {"pct": pct},
    )
    return frozenset(int(r["pid"]) for r in rows)


def ubiquitous_product_ids(pct: float) -> frozenset[int]:
    """Products to exclude from rec/candidate populations: the configured synthetic accounting
    lines (always, e.g. debt-entry 25422404) UNION the data-driven ubiquity set — products bought
    by more than `pct` of distinct clients over the last 12mo on the SAME valid population the
    recommender uses (oi.IsValidForCurrentSale=1). These are universal staples / synthetic lines,
    not cross-sell candidates, and pollute popularity ranking.

    TTL-refreshed (config.ubiquity_cache_ttl) rather than process-lifetime cached, so the set
    tracks the rolling window without a restart. The synthetic ids are pinned unconditionally so
    exclusion never depends on the ubiquity threshold catching them in a given window."""
    s = get_settings()
    now = time.monotonic()
    with _UBIQUITY_LOCK:
        entry = _UBIQUITY_CACHE.get(pct)
        if entry is not None and now - entry[0] < s.ubiquity_cache_ttl:
            return entry[1]
    result = s.synthetic_product_ids | _query_ubiquitous(pct)
    with _UBIQUITY_LOCK:
        _UBIQUITY_CACHE[pct] = (time.monotonic(), result)
    return result


def client_region_id(customer_id: int) -> int | None:
    """The oblast-level region (dbo.Client.RegionID) of a client, resolved via the natural key.

    RegionID is the grouping key (~26 oblasts across ordering clients); RegionCodeID is per-client
    address granularity and does NOT group, so region scoping uses RegionID. Returns None when the
    client has no region set (~1% of ordering clients) — callers then skip scoping (fail-open)."""
    rows = query(
        """
        SELECT c.RegionID AS rid
        FROM dbo.Client c
        WHERE c.ID = :cid
        """,
        {"cid": customer_id},
    )
    if not rows:
        return None
    rid = rows[0]["rid"]
    return int(rid) if rid is not None else None


def count_orders_before(customer_id: int, as_of_date: str) -> int:
    rows = query(
        """
        SELECT COUNT(DISTINCT o.ID) AS n
        FROM dbo.ClientAgreement ca
        JOIN dbo.[Order] o ON ca.ID = o.ClientAgreementID
        WHERE ca.ClientID = :cid AND o.Created < :asof
        """,
        {"cid": customer_id, "asof": as_of_date},
    )
    return int(rows[0]["n"]) if rows else 0


def repurchase_rate(customer_id: int, as_of_date: str) -> float:
    """Share of products bought 2+ times — drives REGULAR sub-segmentation.

    Restricted to the valid rec sales spine (oi.IsValidForCurrentSale = 1) with the synthetic
    accounting lines (e.g. debt-entry 25422404) excluded, matching the rest of the reco spine.
    Without this, synthetic-only / synthetic-dominated clients score an inflated rate (e.g. 1.0)
    and flip REGULAR sub-segmentation on a non-real-product line."""
    synth_ph, synth_params = in_clause("syn", list(get_settings().synthetic_product_ids) or [0])
    rows = query(
        f"""
        SELECT
            COUNT(*) AS total_products,
            SUM(CASE WHEN purchase_count >= 2 THEN 1 ELSE 0 END) AS repurchased
        FROM (
            SELECT oi.ProductID, COUNT(DISTINCT o.ID) AS purchase_count
            FROM dbo.ClientAgreement ca
            JOIN dbo.[Order] o ON ca.ID = o.ClientAgreementID
            JOIN dbo.OrderItem oi ON o.ID = oi.OrderID
            WHERE ca.ClientID = :cid AND o.Created < :asof
                  AND oi.IsValidForCurrentSale = 1
                  AND oi.ProductID IS NOT NULL
                  AND oi.ProductID NOT IN {synth_ph}
            GROUP BY oi.ProductID
        ) t
        """,
        {"cid": customer_id, "asof": as_of_date, **synth_params},
    )
    if not rows or not rows[0]["total_products"]:
        return 0.0
    return float(rows[0]["repurchased"] or 0) / float(rows[0]["total_products"])


def product_frequency(customer_id: int, as_of_date: str) -> dict[int, int]:
    rows = query(
        """
        SELECT oi.ProductID AS pid, COUNT(DISTINCT o.ID) AS cnt
        FROM dbo.ClientAgreement ca
        JOIN dbo.[Order] o ON ca.ID = o.ClientAgreementID
        JOIN dbo.OrderItem oi ON o.ID = oi.OrderID
        WHERE ca.ClientID = :cid AND o.Created < :asof AND oi.ProductID IS NOT NULL
        GROUP BY oi.ProductID
        """,
        {"cid": customer_id, "asof": as_of_date},
    )
    return {int(r["pid"]): int(r["cnt"]) for r in rows}


def product_last_purchase(customer_id: int, as_of_date: str) -> dict[int, object]:
    rows = query(
        """
        SELECT oi.ProductID AS pid, MAX(o.Created) AS last_dt
        FROM dbo.ClientAgreement ca
        JOIN dbo.[Order] o ON ca.ID = o.ClientAgreementID
        JOIN dbo.OrderItem oi ON o.ID = oi.OrderID
        WHERE ca.ClientID = :cid AND o.Created < :asof AND oi.ProductID IS NOT NULL
        GROUP BY oi.ProductID
        """,
        {"cid": customer_id, "asof": as_of_date},
    )
    return {int(r["pid"]): r["last_dt"] for r in rows}


def customer_products(customer_id: int, as_of_date: str, limit: int = 500) -> set[int]:
    """Most-recent N distinct products — for Jaccard similarity (bounded for perf)."""
    rows = query(
        """
        SELECT DISTINCT ProductID FROM (
            SELECT TOP (:lim) oi.ProductID, o.Created
            FROM dbo.ClientAgreement ca
            JOIN dbo.[Order] o ON ca.ID = o.ClientAgreementID
            JOIN dbo.OrderItem oi ON o.ID = oi.OrderID
            WHERE ca.ClientID = :cid AND o.Created < :asof AND oi.ProductID IS NOT NULL
            ORDER BY o.Created DESC
        ) t
        """,
        {"cid": customer_id, "asof": as_of_date, "lim": limit},
    )
    return {int(r["ProductID"]) for r in rows}


def candidate_similar_customers(product_ids: set[int], exclude_id: int, as_of_date: str,
                                limit: int = 400, region_id: int | None = None) -> list[int]:
    """Top-`limit` customers who share the MOST of the target's products (best Jaccard candidates).

    Ranking by overlap (not just DISTINCT membership) both bounds the candidate set for performance
    and keeps the strongest matches, so the downstream batch fetch stays under the SQL parameter cap.

    When `region_id` is given (byRegion scoping), the neighbour pool is restricted to clients in the
    same oblast (dbo.Client.RegionID) — "what clients near me buy". A parameterized JOIN to Client
    keeps the filter inside SQL; passing None leaves behaviour identical to the unscoped query.
    """
    if not product_ids:
        return []
    placeholder, pparams = in_clause("p", list(product_ids))
    region_join = "JOIN dbo.Client cl ON cl.ID = ca.ClientID AND cl.RegionID = :region" \
        if region_id is not None else ""
    extra = {"region": region_id} if region_id is not None else {}
    rows = query(
        f"""
        SELECT TOP (:lim) ca.ClientID AS cid, COUNT(DISTINCT oi.ProductID) AS overlap
        FROM dbo.ClientAgreement ca
        {region_join}
        JOIN dbo.[Order] o ON ca.ID = o.ClientAgreementID
        JOIN dbo.OrderItem oi ON o.ID = oi.OrderID
        WHERE ca.ClientID <> :exclude
              AND o.Created < :asof
              AND oi.ProductID IN {placeholder}
        GROUP BY ca.ClientID
        ORDER BY overlap DESC
        """,
        {"exclude": exclude_id, "asof": as_of_date, "lim": limit, **pparams, **extra},
    )
    return [int(r["cid"]) for r in rows]


def customer_products_bulk(customer_ids: list[int], as_of_date: str) -> dict[int, set[int]]:
    """Distinct products per customer for a batch of customers — ONE query instead of N.

    Replaces the per-candidate round-trip in similarity scoring (the cold-discovery bottleneck:
    ~31s for a HEAVY client became one batched fetch)."""
    if not customer_ids:
        return {}
    placeholder, params = in_clause("c", customer_ids)
    rows = query(
        f"""
        SELECT DISTINCT ca.ClientID AS cid, oi.ProductID AS pid
        FROM dbo.ClientAgreement ca
        JOIN dbo.[Order] o ON ca.ID = o.ClientAgreementID
        JOIN dbo.OrderItem oi ON o.ID = oi.OrderID
        WHERE ca.ClientID IN {placeholder} AND o.Created < :asof AND oi.ProductID IS NOT NULL
        """,
        {"asof": as_of_date, **params},
    )
    out: dict[int, set[int]] = {}
    for r in rows:
        out.setdefault(int(r["cid"]), set()).add(int(r["pid"]))
    return out


def collaborative_products(
    similar: list[tuple[int, float]], owned: set[int], as_of_date: str
) -> dict[int, float]:
    """Products bought by similar customers (weighted by similarity), excluding owned.

    Built with a parameterized VALUES list + IN clause (no string concatenation).
    """
    if not similar:
        return {}
    sim_rows = ",".join(f"(:sc{i}, :sv{i})" for i in range(len(similar)))
    sim_params: dict[str, object] = {}
    for i, (cid, sim) in enumerate(similar):
        sim_params[f"sc{i}"] = cid
        sim_params[f"sv{i}"] = sim
    owned_ph, owned_params = in_clause("o", list(owned) or [0])
    rows = query(
        f"""
        WITH Sim AS (
            SELECT customer_id, similarity FROM (VALUES {sim_rows}) AS t(customer_id, similarity)
        ),
        NeighborProducts AS (
            -- Collapse the Order->OrderItem fan-out to one row per (neighbour, product) BEFORE
            -- weighting, so each neighbour contributes its similarity once per product instead of
            -- once per line. Without this DISTINCT the SUM(similarity) is inflated by line-count.
            -- Scoped to the Sim neighbours so the dedupe only spans the candidate pool.
            SELECT DISTINCT ca.ClientID AS customer_id, oi.ProductID AS pid
            FROM dbo.ClientAgreement ca
            JOIN dbo.[Order] o ON ca.ID = o.ClientAgreementID
            JOIN dbo.OrderItem oi ON o.ID = oi.OrderID
            JOIN Sim s ON ca.ClientID = s.customer_id
            WHERE o.Created < :asof
                  AND oi.ProductID IS NOT NULL
                  AND oi.ProductID NOT IN {owned_ph}
        )
        SELECT np.pid AS pid,
               SUM(s.similarity) / COUNT(DISTINCT s.customer_id) AS score
        FROM NeighborProducts np
        JOIN Sim s ON np.customer_id = s.customer_id
        GROUP BY np.pid
        HAVING COUNT(DISTINCT s.customer_id) >= 2
        """,
        {"asof": as_of_date, **sim_params, **owned_params},
    )
    return {int(r["pid"]): float(r["score"]) for r in rows}


def product_groups(product_ids: list[int]) -> dict[int, int]:
    if not product_ids:
        return {}
    placeholder, pparams = in_clause("p", product_ids)
    rows = query(
        f"""
        SELECT ProductID AS pid, ProductGroupID AS gid
        FROM dbo.ProductProductGroup
        WHERE ProductID IN {placeholder} AND Deleted = 0
        """,
        pparams,
    )
    return {int(r["pid"]): int(r["gid"]) for r in rows}
