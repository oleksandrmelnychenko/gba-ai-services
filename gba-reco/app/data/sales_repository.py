"""Parameterized read queries over confirmed purchases (app.data.source_rules).

All SQL here is parameterized (:name) — no f-string interpolation (prototype anti-pattern).
as_of_date enables point-in-time recommendations for time-split evaluation.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta
from typing import Any

from app.core.config import get_settings
from app.data import source_rules as rules
from app.data.db import in_clause, query

_UBIQUITY_CACHE: dict[tuple[float, str], tuple[float, frozenset[int]]] = {}
_UBIQUITY_LOCK = threading.Lock()

_SYNTHETIC_NAME = "Ввід боргів"
_SYNTHETIC_TTL = 3600.0
_SYNTHETIC_CACHE: tuple[float, frozenset[int]] | None = None
_SYNTHETIC_LOCK = threading.Lock()
_SERVICE_CACHE: tuple[float, frozenset[int]] | None = None
_SERVICE_LOCK = threading.Lock()
_READINESS_TTL = 60.0
_READINESS_CACHE: dict[int, tuple[float, dict[str, Any]]] = {}
_READINESS_LOCK = threading.Lock()
_MEANINGFUL_VENDOR_CODE = r"%[^-./\_ *#+,;:()]%"
_MAX_SIBLING_GROUP = 4
_SIBLING_TTL = 3600.0
_SIBLING_CACHE: tuple[float, dict[int, int]] | None = None
_SIBLING_LOCK = threading.Lock()


def synthetic_product_ids() -> frozenset[int]:
    """Synthetic accounting line(s) — the debt-entry product («Ввід боргів») — excluded
    unconditionally from every recommendation/candidate population.

    Catalog re-syncs re-mint the row under a NEW ID (25422404 → 29555414 → ...), so a hardcoded id
    goes stale; the live id is resolved by Name at runtime and cached for an hour. An empty
    resolution is NOT cached (retried on the next call), and a non-empty
    Settings.synthetic_product_ids (SYNTHETIC_PRODUCT_IDS env) overrides the dynamic lookup."""
    global _SYNTHETIC_CACHE
    override = get_settings().synthetic_product_ids
    if override:
        return override
    now = time.monotonic()
    with _SYNTHETIC_LOCK:
        if _SYNTHETIC_CACHE is not None and now - _SYNTHETIC_CACHE[0] < _SYNTHETIC_TTL:
            return _SYNTHETIC_CACHE[1]
    rows = query(
        """
        SELECT TOP 1 ID AS pid
        FROM dbo.Product
        WHERE Name = :name AND Deleted = 0
        ORDER BY ID DESC
        """,
        {"name": _SYNTHETIC_NAME},
    )
    result = frozenset(int(r["pid"]) for r in rows)
    if result:
        with _SYNTHETIC_LOCK:
            _SYNTHETIC_CACHE = (time.monotonic(), result)
    return result


def service_product_ids() -> frozenset[int]:
    """Live cards that are services, not goods (delivery and the like): flagged by the native
    service marker, named «Послуг…» or measured in «послуга». They can carry a stock balance
    and sell often, so stock and popularity filters do not remove them."""
    global _SERVICE_CACHE
    now = time.monotonic()
    with _SERVICE_LOCK:
        if _SERVICE_CACHE is not None and now - _SERVICE_CACHE[0] < _SYNTHETIC_TTL:
            return _SERVICE_CACHE[1]
    rows = query(
        """
        SELECT p.ID AS pid
        FROM dbo.Product p
        LEFT JOIN dbo.MeasureUnit mu ON mu.ID = p.MeasureUnitID
        WHERE p.Deleted = 0
              AND (p.NativeIsService = 1 OR LTRIM(p.Name) LIKE :service_name
                   OR LTRIM(RTRIM(mu.Name)) = :service_unit)
        """,
        {"service_name": "Послуг%", "service_unit": "послуга"},
    )
    result = frozenset(int(r["pid"]) for r in rows)
    with _SERVICE_LOCK:
        _SERVICE_CACHE = (time.monotonic(), result)
    return result


def source_readiness(max_lag_days: int) -> dict[str, Any]:
    """Cached business-source probe for health/readiness endpoints.

    Connectivity alone is not readiness: the service also needs a current valid sales spine,
    the dynamic synthetic exclusion, and positive stock on an operational resale storage.
    """
    now_mono = time.monotonic()
    with _READINESS_LOCK:
        cached = _READINESS_CACHE.get(max_lag_days)
        if cached is not None and now_mono - cached[0] < _READINESS_TTL:
            return dict(cached[1])

    synthetic_ids = synthetic_product_ids()
    synth_ph, synth_params = in_clause("health_synth", list(synthetic_ids) or [0])
    today = datetime.combine(datetime.now(rules.BUSINESS_ZONE).date(), datetime.min.time())
    rows = query(
        f"""
        SELECT
            (
                SELECT TOP 1 {rules.SOLD_AT}
                FROM {rules.SPINE}
                JOIN dbo.Client c ON c.ID = ca.ClientID AND c.Deleted = 0
                WHERE {rules.PURCHASED}
                      AND oi.ProductID NOT IN {synth_ph}
                ORDER BY {rules.SALE_INSTANT} DESC, s.ID DESC
            ) AS latest_sale_at,
            (
                SELECT COUNT_BIG(DISTINCT pa.ProductID)
                FROM dbo.ProductAvailability pa
                JOIN dbo.Product p ON p.ID = pa.ProductID AND p.Deleted = 0
                JOIN dbo.Storage s ON s.ID = pa.StorageID
                WHERE pa.Deleted = 0 AND pa.Amount > 0
                      AND {rules.sellable_storage("s")}
            ) AS stocked_product_count,
            (
                SELECT COUNT_BIG(*)
                FROM dbo.Storage s
                WHERE {rules.sellable_storage("s")}
            ) AS sellable_storage_count
        """,
        {**rules.window(today + timedelta(days=1)), **synth_params},
    )
    row = rows[0] if rows else {}
    latest = row.get("latest_sale_at")
    fresh = isinstance(latest, datetime) and latest >= today - timedelta(days=max_lag_days)
    stocked_count = int(row.get("stocked_product_count") or 0)
    storage_count = int(row.get("sellable_storage_count") or 0)
    reasons: list[str] = []
    if not synthetic_ids:
        reasons.append("synthetic_product_unresolved")
    if latest is None:
        reasons.append("valid_sales_missing")
    elif not fresh:
        reasons.append("valid_sales_stale")
    if storage_count <= 0:
        reasons.append("sellable_storage_missing")
    if stocked_count <= 0:
        reasons.append("sellable_stock_missing")

    result = {
        "business_ready": not reasons,
        "reasons": reasons,
        "latest_sale_at": latest.isoformat() if isinstance(latest, datetime) else None,
        "stocked_product_count": stocked_count,
        "sellable_storage_count": storage_count,
        "synthetic_product_count": len(synthetic_ids),
    }
    with _READINESS_LOCK:
        _READINESS_CACHE[max_lag_days] = (time.monotonic(), dict(result))
    return result


def sibling_card_rows() -> tuple[list[tuple[int, str, str]], list[tuple[str, int]]]:
    """Live cards of one article, by gba-server's sibling rule (ProductSiblingRepository):
    equal meaningful VendorCode on at most four live cards with the same first word of the
    name, plus the manual links of dbo.ProductSiblingLink."""
    auto = query(
        """
        SELECT m.ID AS pid, m.VendorCode AS code,
               LEFT(LTRIM(m.Name), CHARINDEX(N' ', LTRIM(m.Name) + N' ') - 1) AS name_key
        FROM dbo.Product m
        JOIN (SELECT VendorCode FROM dbo.Product WHERE Deleted = 0
              GROUP BY VendorCode HAVING COUNT(*) BETWEEN 2 AND :max_size) g
             ON g.VendorCode = m.VendorCode
        WHERE m.Deleted = 0 AND m.VendorCode LIKE :meaningful
        """,
        {"max_size": _MAX_SIBLING_GROUP, "meaningful": _MEANINGFUL_VENDOR_CODE},
    )
    manual: list[dict[str, Any]] = []
    ready = query(
        "SELECT CASE WHEN OBJECT_ID(N'dbo.ProductSiblingLink', N'U') IS NOT NULL "
        "THEN 1 ELSE 0 END AS ready"
    )
    if ready and ready[0]["ready"]:
        manual = query(
            """
            SELECT l.GroupKey AS gkey, p.ID AS pid
            FROM dbo.ProductSiblingLink l
            JOIN dbo.Product p ON p.SourceAmgCode = l.SourceCode AND p.Deleted = 0
            WHERE l.SourceWorld = 'AMG'
            UNION
            SELECT l.GroupKey AS gkey, p.ID AS pid
            FROM dbo.ProductSiblingLink l
            JOIN dbo.Product p ON p.SourceFenixCode = l.SourceCode AND p.Deleted = 0
            WHERE l.SourceWorld = 'FENIX'
            """
        )
    return (
        [(int(r["pid"]), str(r["code"]), str(r["name_key"] or "")) for r in auto],
        [(str(r["gkey"]), int(r["pid"])) for r in manual],
    )


def group_siblings(
    auto: list[tuple[int, str, str]], manual: list[tuple[str, int]],
) -> dict[int, int]:
    """Card id -> article id (the lowest card id of its sibling group), grouped cards only."""
    parent: dict[int, int] = {}

    def find(pid: int) -> int:
        parent.setdefault(pid, pid)
        while parent[pid] != pid:
            parent[pid] = parent[parent[pid]]
            pid = parent[pid]
        return pid

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    buckets: dict[tuple[str, str], list[int]] = {}
    for pid, code, name_key in auto:
        buckets.setdefault((code, name_key), []).append(pid)
    for gkey, pid in manual:
        buckets.setdefault((gkey, "\x00manual"), []).append(pid)
    for members in buckets.values():
        for pid in members[1:]:
            union(members[0], pid)
    sizes: dict[int, int] = {}
    for pid in list(parent):
        sizes[find(pid)] = sizes.get(find(pid), 0) + 1
    return {pid: find(pid) for pid in parent if sizes[find(pid)] > 1}


def sibling_articles() -> dict[int, int]:
    global _SIBLING_CACHE
    now = time.monotonic()
    with _SIBLING_LOCK:
        if _SIBLING_CACHE is not None and now - _SIBLING_CACHE[0] < _SIBLING_TTL:
            return _SIBLING_CACHE[1]
    result = group_siblings(*sibling_card_rows())
    with _SIBLING_LOCK:
        _SIBLING_CACHE = (time.monotonic(), result)
    return result


def with_siblings(product_ids: set[int] | frozenset[int], articles: dict[int, int]) -> set[int]:
    wanted = {articles[pid] for pid in product_ids if pid in articles}
    if not wanted:
        return set(product_ids)
    return set(product_ids) | {pid for pid, article in articles.items() if article in wanted}


def open_order_rows(as_of: str) -> list[tuple[int, int]]:
    rows = query(
        f"""
        SELECT DISTINCT ca.ClientID AS cid, oi.ProductID AS pid
        FROM {rules.SPINE}
        WHERE {rules.OPEN_ORDER}
        """,
        rules.open_window(as_of, get_settings().open_order_days),
    )
    return [(int(r["cid"]), int(r["pid"])) for r in rows]


def open_order_product_ids(customer_id: int, as_of: str) -> frozenset[int]:
    """Products in the client's open orders, with their sibling cards: already in the basket,
    so never recommended."""
    rows = query(
        f"""
        SELECT DISTINCT oi.ProductID AS pid
        FROM {rules.SPINE}
        WHERE ca.ClientID = :cid AND {rules.OPEN_ORDER}
        """,
        {"cid": customer_id, **rules.open_window(as_of, get_settings().open_order_days)},
    )
    found = {int(r["pid"]) for r in rows}
    return frozenset(with_siblings(found, sibling_articles())) if found else frozenset()


def client_exists(customer_id: int) -> bool:
    """Whether the requested current client identity exists."""
    rows = query(
        """
        SELECT TOP 1 1 AS found
        FROM dbo.Client
        WHERE ID = :cid AND Deleted = 0 AND NetUID IS NOT NULL
        """,
        {"cid": customer_id},
    )
    return bool(rows)


def active_product_ids(product_ids: list[int]) -> set[int]:
    """Requested ids that are current, non-synthetic catalog products."""
    if not product_ids:
        return set()
    ph, params = in_clause("active_product", list(dict.fromkeys(product_ids)))
    synth_ph, synth_params = in_clause(
        "active_synth", list(synthetic_product_ids()) or [0]
    )
    rows = query(
        f"""
        SELECT ID AS pid
        FROM dbo.Product
        WHERE ID IN {ph} AND Deleted = 0 AND ID NOT IN {synth_ph}
        """,
        {**params, **synth_params},
    )
    return {int(row["pid"]) for row in rows}


def _query_ubiquitous(pct: float, as_of: str) -> frozenset[int]:
    rows = query(
        f"""
        WITH base AS (
            SELECT ca.ClientID AS cid, oi.ProductID AS pid
            FROM {rules.SPINE}
            WHERE {rules.PURCHASED}
        ),
        tot AS (SELECT COUNT(DISTINCT cid) AS n FROM base)
        SELECT b.pid AS pid
        FROM base b CROSS JOIN tot
        GROUP BY b.pid, tot.n
        HAVING COUNT(DISTINCT b.cid) * 1.0 / NULLIF(tot.n, 0) > :pct
        """,
        {"pct": pct, **rules.window(as_of, rules.trailing_year(as_of))},
    )
    return frozenset(int(r["pid"]) for r in rows)


def ubiquitous_product_ids(pct: float, as_of: str | None = None) -> frozenset[int]:
    """Products to exclude from rec/candidate populations: the synthetic accounting
    lines (always, e.g. the debt-entry line — see synthetic_product_ids) UNION the data-driven
    ubiquity set — products bought
    by more than `pct` of distinct clients over the last 12mo on the SAME valid population the
    recommender uses (source_rules.PURCHASED). These are universal staples / synthetic lines,
    not cross-sell candidates, and pollute popularity ranking.

    TTL-refreshed (config.ubiquity_cache_ttl) rather than process-lifetime cached, so the set
    tracks the rolling window without a restart. The synthetic ids are pinned unconditionally so
    exclusion never depends on the ubiquity threshold catching them in a given window."""
    s = get_settings()
    as_of = as_of or rules.today()
    now = time.monotonic()
    with _UBIQUITY_LOCK:
        entry = _UBIQUITY_CACHE.get((pct, as_of))
        if entry is not None and now - entry[0] < s.ubiquity_cache_ttl:
            return entry[1]
    result = synthetic_product_ids() | service_product_ids() | _query_ubiquitous(pct, as_of)
    with _UBIQUITY_LOCK:
        if len(_UBIQUITY_CACHE) > 512:
            _UBIQUITY_CACHE.clear()
        _UBIQUITY_CACHE[(pct, as_of)] = (time.monotonic(), result)
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


def client_net_uid(customer_id: int) -> str | None:
    """Client.NetUID (the 1C natural key) for a client id — ANY generation, deleted or not,
    so a stale id from before a client re-sync still resolves to the same identity."""
    rows = query(
        "SELECT NetUID AS uid FROM dbo.Client WHERE ID = :cid",
        {"cid": customer_id},
    )
    if not rows or rows[0]["uid"] is None:
        return None
    return str(rows[0]["uid"]).lower()


def product_vendor_codes(product_ids: list[int]) -> list[str]:
    """Distinct VendorCode natural keys for product ids (any catalog generation).

    Codes shared by MORE than one live row are skipped: placeholder codes like «-» sit on
    hundreds of unrelated live products, so they identify nothing — storing one as a negative
    would blast-exclude the whole junk-code cohort at read time. A real re-mint lineage has at
    most one live row per code."""
    if not product_ids:
        return []
    ph, params = in_clause("p", list(dict.fromkeys(product_ids)))
    rows = query(
        f"""
        SELECT DISTINCT p.VendorCode AS vc
        FROM dbo.Product p
        WHERE p.ID IN {ph} AND p.VendorCode IS NOT NULL AND p.VendorCode <> ''
              AND (SELECT COUNT(*) FROM dbo.Product l
                   WHERE l.VendorCode = p.VendorCode AND l.Deleted = 0) <= 1
        """,
        params,
    )
    return [str(r["vc"]) for r in rows]


def product_ids_for_vendor_codes(vendor_codes: list[str]) -> set[int]:
    """EVERY Product.ID generation (live AND soft-deleted) carrying one of the VendorCodes.

    Used to expand stored negative-feedback VendorCodes into an exclusion set: candidate pools
    are built from order history BEFORE live_remap runs, so excluding only the live id would let
    a dead-generation candidate slip through and be remapped onto the negatived live row."""
    if not vendor_codes:
        return set()
    ph, params = in_clause("v", list(dict.fromkeys(vendor_codes)))
    rows = query(
        f"SELECT ID AS pid FROM dbo.Product WHERE VendorCode IN {ph}",
        params,
    )
    return {int(r["pid"]) for r in rows}


def count_orders_before(
    customer_id: int, as_of_date: str, client_agreement_net_id: str | None = None,
) -> int:
    rows = query(
        f"""
        SELECT COUNT(DISTINCT o.ID) AS n
        FROM {rules.SPINE}
        WHERE ca.ClientID = :cid
                  AND (:agreement_uid IS NULL OR ca.NetUID = CONVERT(uniqueidentifier, :agreement_uid))
                  AND {rules.PURCHASED}
        """,
        {
            "cid": customer_id,
            "agreement_uid": client_agreement_net_id,
            **rules.window(as_of_date),
        },
    )
    return int(rows[0]["n"]) if rows else 0


def repurchase_rate(
    customer_id: int, as_of_date: str, client_agreement_net_id: str | None = None,
) -> float:
    """Share of products bought 2+ times — drives REGULAR sub-segmentation.

    Restricted to confirmed purchases (source_rules.PURCHASED) with the synthetic
    accounting lines (the debt-entry line) excluded, matching the rest of the reco spine.
    Without this, synthetic-only / synthetic-dominated clients score an inflated rate (e.g. 1.0)
    and flip REGULAR sub-segmentation on a non-real-product line."""
    synth_ph, synth_params = in_clause("syn", list(synthetic_product_ids()) or [0])
    rows = query(
        f"""
        SELECT
            COUNT(*) AS total_products,
            SUM(CASE WHEN purchase_count >= 2 THEN 1 ELSE 0 END) AS repurchased
        FROM (
            SELECT oi.ProductID, COUNT(DISTINCT o.ID) AS purchase_count
            FROM {rules.SPINE}
            WHERE ca.ClientID = :cid
                  AND (:agreement_uid IS NULL OR ca.NetUID = CONVERT(uniqueidentifier, :agreement_uid))
                  AND {rules.PURCHASED}
                  AND oi.ProductID NOT IN {synth_ph}
            GROUP BY oi.ProductID
        ) t
        """,
        {
            "cid": customer_id,
            "agreement_uid": client_agreement_net_id,
            **rules.window(as_of_date),
            **synth_params,
        },
    )
    if not rows or not rows[0]["total_products"]:
        return 0.0
    return float(rows[0]["repurchased"] or 0) / float(rows[0]["total_products"])


def product_frequency(
    customer_id: int, as_of_date: str, client_agreement_net_id: str | None = None,
) -> dict[int, int]:
    rows = query(
        f"""
        SELECT oi.ProductID AS pid, COUNT(DISTINCT o.ID) AS cnt
        FROM {rules.SPINE}
        WHERE ca.ClientID = :cid
                  AND (:agreement_uid IS NULL OR ca.NetUID = CONVERT(uniqueidentifier, :agreement_uid))
                  AND {rules.PURCHASED}
        GROUP BY oi.ProductID
        """,
        {
            "cid": customer_id,
            "agreement_uid": client_agreement_net_id,
            **rules.window(as_of_date),
        },
    )
    return {int(r["pid"]): int(r["cnt"]) for r in rows}


def product_last_purchase(
    customer_id: int, as_of_date: str, client_agreement_net_id: str | None = None,
) -> dict[int, object]:
    rows = query(
        f"""
        SELECT oi.ProductID AS pid, MAX({rules.SOLD_AT}) AS last_dt
        FROM {rules.SPINE}
        WHERE ca.ClientID = :cid
                  AND (:agreement_uid IS NULL OR ca.NetUID = CONVERT(uniqueidentifier, :agreement_uid))
                  AND {rules.PURCHASED}
        GROUP BY oi.ProductID
        """,
        {
            "cid": customer_id,
            "agreement_uid": client_agreement_net_id,
            **rules.window(as_of_date),
        },
    )
    return {int(r["pid"]): r["last_dt"] for r in rows}


def owned_live_product_ids(
    customer_id: int, as_of_date: str, client_agreement_net_id: str | None = None,
) -> set[int]:
    """Live catalog identities of every product previously bought by the client.

    Order history frequently points at soft-deleted product generations after a catalog
    re-sync. Comparing raw ProductID values would therefore let a newer generation of an
    already-bought product leak into discovery. Resolve each historical row with the exact
    same newest-live-VendorCode rule as ``live_remap``. The whole lookup stays in SQL, avoiding
    an unbounded ProductID ``IN`` list for clients with wide histories. Sibling cards of a bought
    product are the same article, so they count as bought too.
    """
    rows = query(
        f"""
        WITH Owned AS (
            SELECT DISTINCT oi.ProductID AS historical_id
            FROM {rules.SPINE}
            WHERE ca.ClientID = :cid
                  AND (:agreement_uid IS NULL OR ca.NetUID = CONVERT(uniqueidentifier, :agreement_uid))
                  AND {rules.PURCHASED}
        )
        SELECT DISTINCT
               CASE WHEN historical.Deleted = 0
                    THEN historical.ID
                    ELSE live.live_id
               END AS pid
        FROM Owned owned
        JOIN dbo.Product historical ON historical.ID = owned.historical_id
        OUTER APPLY (
            SELECT TOP 1 current_product.ID AS live_id
            FROM dbo.Product current_product
            WHERE historical.Deleted <> 0
                  AND current_product.VendorCode = historical.VendorCode
                  AND current_product.Deleted = 0
            ORDER BY current_product.ID DESC
        ) live
        WHERE historical.Deleted = 0 OR live.live_id IS NOT NULL
        """,
        {
            "cid": customer_id,
            "agreement_uid": client_agreement_net_id,
            **rules.window(as_of_date),
        },
    )
    return with_siblings({int(row["pid"]) for row in rows}, sibling_articles())


def customer_products(
    customer_id: int, as_of_date: str, limit: int = 500, client_agreement_net_id: str | None = None,
) -> set[int]:
    """Most-recent N distinct products — for Jaccard similarity (bounded for perf)."""
    rows = query(
        f"""
        SELECT DISTINCT ProductID FROM (
            SELECT TOP (:lim) oi.ProductID, {rules.SALE_INSTANT} AS sold_at
            FROM {rules.SPINE}
            WHERE ca.ClientID = :cid
                  AND (:agreement_uid IS NULL OR ca.NetUID = CONVERT(uniqueidentifier, :agreement_uid))
                  AND {rules.PURCHASED}
            ORDER BY {rules.SALE_INSTANT} DESC, o.ID DESC, oi.ProductID ASC
        ) t
        """,
        {
            "cid": customer_id,
            "agreement_uid": client_agreement_net_id,
            "lim": limit,
            **rules.window(as_of_date),
        },
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
        FROM {rules.SPINE}
        {region_join}
        WHERE ca.ClientID <> :exclude
              AND {rules.PURCHASED}
              AND oi.ProductID IN {placeholder}
        GROUP BY ca.ClientID
        ORDER BY overlap DESC, ca.ClientID ASC
        """,
        {
            "exclude": exclude_id,
            "lim": limit,
            **rules.window(as_of_date),
            **pparams,
            **extra,
        },
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
        FROM {rules.SPINE}
        WHERE ca.ClientID IN {placeholder}
              AND {rules.PURCHASED}
        """,
        {
            **rules.window(as_of_date),
            **params,
        },
    )
    out: dict[int, set[int]] = {}
    for r in rows:
        out.setdefault(int(r["cid"]), set()).add(int(r["pid"]))
    return out


def collaborative_products(
    similar: list[tuple[int, float]], as_of_date: str, customer_id: int,
    client_agreement_net_id: str | None = None,
) -> dict[int, float]:
    """Products bought by similar customers (weighted by similarity), excluding owned.

    Owned products are excluded server-side via NOT EXISTS against the target's own order
    history (computed in-SQL from customer_id), so no per-product parameter list is sent —
    a client with a very wide history no longer overflows the driver's parameter budget.
    """
    if not similar:
        return {}
    sim_rows = ",".join(f"(:sc{i}, :sv{i})" for i in range(len(similar)))
    sim_params: dict[str, object] = {}
    for i, (cid, sim) in enumerate(similar):
        sim_params[f"sc{i}"] = cid
        sim_params[f"sv{i}"] = sim
    rows = query(
        f"""
        WITH Owned AS (
            SELECT DISTINCT oi.ProductID AS pid
            FROM {rules.SPINE}
            WHERE ca.ClientID = :cid
                  AND (:agreement_uid IS NULL OR ca.NetUID = CONVERT(uniqueidentifier, :agreement_uid))
                  AND {rules.PURCHASED}
        ),
        Sim AS (
            SELECT customer_id, similarity FROM (VALUES {sim_rows}) AS t(customer_id, similarity)
        ),
        NeighborProducts AS (
            -- Collapse the Order->OrderItem fan-out to one row per (neighbour, product) BEFORE
            -- weighting, so each neighbour contributes its similarity once per product instead of
            -- once per line. Without this DISTINCT the SUM(similarity) is inflated by line-count.
            -- Scoped to the Sim neighbours so the dedupe only spans the candidate pool.
            SELECT DISTINCT ca.ClientID AS customer_id, oi.ProductID AS pid
            FROM {rules.SPINE}
            JOIN Sim sim ON ca.ClientID = sim.customer_id
            WHERE {rules.PURCHASED}
                  AND NOT EXISTS (SELECT 1 FROM Owned ow WHERE ow.pid = oi.ProductID)
        )
        SELECT np.pid AS pid,
               SUM(sim.similarity) / COUNT(DISTINCT sim.customer_id) AS score
        FROM NeighborProducts np
        JOIN Sim sim ON np.customer_id = sim.customer_id
        GROUP BY np.pid
        HAVING COUNT(DISTINCT sim.customer_id) >= 2
        """,
        {
            "cid": customer_id,
            "agreement_uid": client_agreement_net_id,
            **rules.window(as_of_date),
            **sim_params,
        },
    )
    return {int(r["pid"]): float(r["score"]) for r in rows}


def active_agreements(customer_id: int) -> list[str]:
    """Current agreements owned by the client, with a usable organization context."""
    rows = query(
        """
        SELECT ca.NetUID AS uid
        FROM dbo.ClientAgreement ca
        JOIN dbo.Client c ON c.ID = ca.ClientID AND c.Deleted = 0
        JOIN dbo.Agreement a ON a.ID = ca.AgreementID
        JOIN dbo.Organization org ON org.ID = a.OrganizationID AND org.Deleted = 0
        WHERE ca.ClientID = :cid AND ca.Deleted = 0 AND ca.NetUID IS NOT NULL
              AND a.Deleted = 0 AND a.IsActive = 1
        ORDER BY ca.ID
        """,
        {"cid": customer_id},
    )
    return [str(row["uid"]).lower() for row in rows]


def in_stock_product_ids(product_ids: list[int], client_agreement_net_id: str | None = None) -> set[int]:
    """Subset whose exact live catalog row has stock on an operational resale storage.

    Candidate ids come from order history, which can carry soft-deleted catalog generations;
    stock lives on the live row, so the check bridges generations via the VendorCode natural key
    using the same newest-live-row rule as ``live_remap``. ONE set-membership query is used for
    the whole candidate pool, never one query per product.
    """
    if not product_ids:
        return set()
    if client_agreement_net_id is not None:
        return _agreement_stock_product_ids(product_ids, client_agreement_net_id)
    ph, params = in_clause("p", list(dict.fromkeys(product_ids)))
    rows = query(
        f"""
        WITH live_map AS (
            SELECT d.ID AS old_id,
                   CASE WHEN d.Deleted = 0 THEN d.ID ELSE MAX(l.ID) END AS live_id
            FROM dbo.Product d
            JOIN dbo.Product l ON l.VendorCode = d.VendorCode AND l.Deleted = 0
            WHERE d.ID IN {ph} AND d.VendorCode IS NOT NULL AND d.VendorCode <> ''
            GROUP BY d.ID, d.Deleted
        )
        SELECT DISTINCT m.old_id AS pid
        FROM live_map m
        JOIN dbo.ProductAvailability pa
             ON pa.ProductID = m.live_id AND pa.Deleted = 0 AND pa.Amount > 0
        JOIN dbo.Storage s ON s.ID = pa.StorageID
        WHERE {rules.sellable_storage("s")}
        """,
        params,
    )
    return {int(r["pid"]) for r in rows}


def _agreement_stock_product_ids(product_ids: list[int], agreement_uid: str) -> set[int]:
    """Ukraine sale buckets, matching ProductAvailabilityBucketPolicy and the sale wizard.

    VAT uses only the agreement's own storage; non-VAT also accepts Ukrainian
    resale storage. Defective/Polish stock cannot make a Ukrainian sale actionable.
    Final prices are calculated by gba-server's existing agreement pricing path.
    """
    ph, params = in_clause("p", list(dict.fromkeys(product_ids)))
    rows = query(
        f"""
        WITH live_map AS (
            SELECT d.ID AS old_id,
                   CASE WHEN d.Deleted = 0 THEN d.ID ELSE MAX(l.ID) END AS live_id
            FROM dbo.Product d
            JOIN dbo.Product l ON l.VendorCode = d.VendorCode AND l.Deleted = 0
            WHERE d.ID IN {ph} AND d.VendorCode IS NOT NULL AND d.VendorCode <> ''
            GROUP BY d.ID, d.Deleted
        )
        SELECT m.old_id AS pid
        FROM live_map m
        JOIN dbo.ProductAvailability pa ON pa.ProductID = m.live_id AND pa.Deleted = 0
        JOIN dbo.Storage s ON s.ID = pa.StorageID AND s.Deleted = 0
        JOIN dbo.ClientAgreement ca ON ca.NetUID = CONVERT(uniqueidentifier, :agreement_uid)
             AND ca.Deleted = 0
        JOIN dbo.Agreement a ON a.ID = ca.AgreementID AND a.Deleted = 0 AND a.IsActive = 1
        JOIN dbo.Organization org ON org.ID = a.OrganizationID AND org.Deleted = 0
        WHERE s.ForDefective = 0 AND COALESCE(LOWER(s.Locale), '') <> 'pl'
              AND (s.ID = org.StorageID OR (a.WithVATAccounting = 0 AND s.AvailableForReSale = 1))
        GROUP BY m.old_id
        HAVING SUM(pa.Amount) > 0
        """,
        {**params, "agreement_uid": agreement_uid},
    )
    return {int(row["pid"]) for row in rows}


def product_groups(product_ids: list[int]) -> dict[int, int]:
    if not product_ids:
        return {}
    placeholder, pparams = in_clause("p", product_ids)
    rows = query(
        f"""
        SELECT ProductID AS pid, ProductGroupID AS gid
        FROM dbo.ProductProductGroup
        WHERE ProductID IN {placeholder} AND Deleted = 0
        ORDER BY ProductID, ProductGroupID
        """,
        pparams,
    )
    return {int(r["pid"]): int(r["gid"]) for r in rows}
