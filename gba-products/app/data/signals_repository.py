"""Read-only product-intelligence signals over ConcordDb_V5. All parameterized.

LOAD-BEARING DATA RULES (verified on ConcordDb_V5):
  - Available sellable stock = ProductAvailability.Amount in active, non-defective operational
    storages marked AvailableForReSale or IsResale. Amount is already NET/FREE: gba-server subtracts
    ProductReservation.Qty when reserving and adds it back on release. Never subtract reservations
    again here and never reconstruct gross stock for this availability-facing service.
  - ReSaleAvailability is NOT a stock source. It is an optional cost-layer table and is currently
    empty after the 1C rebuild while ProductAvailability is populated. Depending on it made every
    stocked SKU look like zero stock despite a healthy DB connection.
  - EUR valuation uses a quantity-weighted AccountingPrice over active non-debt ConsignmentItem
    cost lots, joined only after ProductAvailability has been aggregated per product. This prevents
    cost-lot rows from multiplying the operational stock amount. A product with no factual cost lot
    keeps NULL valuation/cost; quantity remains exact.
  - 1С DEBT-IMPORT lot contamination (verified live; mirrors gba-pricing unit_cost_eur exactly):
    a lot whose dbo.ProductIncome.SourceDocumentType = 1 (the 1С debt/balance-import document) carries
    an inflated debt-injection AccountingPrice (~55x the real cost) on BOTH Consignment.IsImportedFromOneC
    and IsVirtual lots — neither Consignment flag isolates it. These lots inflated the on-hand EUR value
    ~3x (€985k contaminated vs €323k real; 67.2% / €662k of the total came from srcDoc=1 lots, touching
    357 products) and overstated per-product unit_cost up to 11.5x (pid 26157549: 1.0406 -> 0.0938).
    So the COST CTE joins ConsignmentItem -> Consignment (ci.ConsignmentID) -> ProductIncome
    (c.ProductIncomeID) and excludes SourceDocumentType=1 from the derived cost (a lot with no
    ProductIncome — e.g. a pure transfer — is kept via pi.ID IS NULL). Stock quantity itself comes
    only from ProductAvailability and is not deleted merely because a cost layer is unavailable.
    Legacy PI docs (pre-source-identity era) carry SourceDocumentType NULL and are KEPT: the exclusion is
    only for positively identified type-1 docs, and NULL <> 1 evaluates to NULL in SQL — the predicate
    must spell out IS NULL or the untyped historical lot layer silently vanishes from every stock signal.
    gba-pricing (pricing_repository.unit_cost_eur) and gba-procure deliberately exclude these too.
  - Sellable warehouses: Storage.Deleted=0 AND ForDefective=0 AND (AvailableForReSale=1 OR IsResale=1).
  - Sales use the native report fact set: confirmed active Sale, active OrderItem, the sale's
    own ClientAgreement, and Sale.EffectiveDate. Accounting-owned balance carriers are excluded
    by immutable provenance, never by a guessed synthetic product ID or deletion statistics.
  - Gross EUR remains gross (before returns) for ABC/average price. Net EUR is a separate signed
    measure. Active returns use their own FromDate and actual Amount with generated currency proof.
  - Every calendar boundary is a Europe/Kyiv midnight converted independently to UTC.

"""
from __future__ import annotations

import time
from collections.abc import Sequence
from typing import Any

from app.core import exact_numbers as exact
from app.core.config import get_settings
from app.core.history import (
    HistoryWindow,
    day_history_window,
    month_history_window,
    resolve_history_window,
)
from app.data.db import in_clause, query
from app.data import canonical_sales as canonical
from app.core.history import kyiv_midnight_utc

# 1С debt/balance-import document type on dbo.ProductIncome.SourceDocumentType. Such lots carry an
# inflated balance-import AccountingPrice across BOTH Consignment.IsImportedFromOneC and IsVirtual;
# they are excluded from the derived unit cost. Operational quantity remains ProductAvailability.
_DEBT_IMPORT_SOURCE_DOCUMENT_TYPE = 1

_SELLABLE_STORAGE = (
    "s.Deleted = 0 AND s.ForDefective = 0 "
    "AND (s.AvailableForReSale = 1 OR s.IsResale = 1)"
)

_PA_QTY = "CAST(pa.Amount AS decimal(18, 8))"
_COST_QTY = "CAST(ci.RemainingQty AS decimal(18, 8))"
_COST_PRICE = "CAST(ci.AccountingPrice AS decimal(28, 14))"
# Legacy synthetic-product identity is retained for startup diagnostics only.
# Financial population comes from native confirmed-sale ownership rules, never a
# mutable product name/ID exclusion. The resolver does not filter these aggregates.
_SYNTHETIC_FALLBACK_ID = 29555414
_SYNTHETIC_REFRESH_SECONDS = 3600
_synthetic_cached: tuple[int, float] | None = None


def _window_params(as_of: str, window: HistoryWindow) -> dict[str, Any]:
    return {
        "asof": kyiv_midnight_utc(as_of),
        "source_history_start": kyiv_midnight_utc(window.source_history_start),
        "history_start": kyiv_midnight_utc(window.effective_start),
    }


def _day_history_params(as_of: str, window_days: int) -> tuple[HistoryWindow, dict[str, Any]]:
    window = day_history_window(
        as_of,
        window_days,
        get_settings().source_history_start_date,
    )
    return window, _window_params(as_of, window)


def _month_history_params(as_of: str, months: int) -> tuple[HistoryWindow, dict[str, Any]]:
    window = month_history_window(
        as_of,
        months,
        get_settings().source_history_start_date,
    )
    return window, _window_params(as_of, window)


def _explicit_history_params(
    as_of: str,
    requested_start: str,
) -> tuple[HistoryWindow, dict[str, Any]]:
    window = resolve_history_window(
        as_of,
        requested_start,
        get_settings().source_history_start_date,
    )
    return window, _window_params(as_of, window)


def synthetic_product_id() -> int:
    """Current dbo.Product.ID of the synthetic «Ввід боргів» debt-entry product (cached ~1h)."""
    global _synthetic_cached
    override = get_settings().synthetic_product_id
    if override:
        return int(override)
    now = time.monotonic()
    if _synthetic_cached is not None and now - _synthetic_cached[1] < _SYNTHETIC_REFRESH_SECONDS:
        return _synthetic_cached[0]
    try:
        rows = query(
            "SELECT TOP 1 ID AS id FROM dbo.Product "
            "WHERE Name = N'Ввід боргів' AND Deleted = 0 ORDER BY ID DESC"
        )
    except Exception:  # noqa: BLE001
        rows = []
    if rows:
        _synthetic_cached = (int(rows[0]["id"]), now)
    elif _synthetic_cached is None:
        return _SYNTHETIC_FALLBACK_ID
    else:
        _synthetic_cached = (_synthetic_cached[0], now)
    return _synthetic_cached[0]


def _in_filter(col: str, name: str, product_ids: Sequence[int] | None,
               params: dict[str, Any]) -> str:
    """Optional small-set IN filter. Bulk callers pass None and aggregate over the full set
    (SQL Server caps parameters at ~2100, so large id lists must use the None path)."""
    if not product_ids:
        return ""
    if len(product_ids) > 2000:
        raise ValueError("product_ids too large for an IN clause; use the bulk (None) path")
    ph, p = in_clause(name, [int(x) for x in product_ids])
    params.update(p)
    return f" AND {col} IN {ph}"


def on_hand_stock(product_ids: Sequence[int] | None = None) -> list[dict]:
    """Per-product current NET/FREE stock in operational sellable storages plus EUR valuation.

    ``ProductAvailability.Amount`` is returned unchanged apart from summing storage rows. It
    already reflects active reservations, so this query intentionally never joins
    ``ProductReservation``. Cost lots are aggregated separately and cannot multiply stock.
    """
    params: dict[str, Any] = {
        "debt_doc_type": _DEBT_IMPORT_SOURCE_DOCUMENT_TYPE,
        "synth": synthetic_product_id(),
    }
    flt = _in_filter("pa.ProductID", "p", product_ids, params)
    return query(
        f"""
        WITH OperationalStock AS (
            SELECT pa.ProductID AS product_id,
                   SUM({_PA_QTY}) AS qty_on_hand
            FROM dbo.ProductAvailability pa
            JOIN dbo.Storage s ON s.ID = pa.StorageID
            WHERE pa.Deleted = 0
                  AND pa.ProductID IS NOT NULL
                  AND pa.ProductID <> :synth
                  AND {_SELLABLE_STORAGE}{flt}
            GROUP BY pa.ProductID
            HAVING SUM({_PA_QTY}) > 0
        ),
        CostLots AS (
            SELECT ci.ProductID AS product_id,
                   SUM({_COST_QTY} * {_COST_PRICE}) AS cost_value_eur,
                   SUM({_COST_QTY}) AS cost_qty
            FROM dbo.ConsignmentItem ci
            JOIN OperationalStock stock ON stock.product_id = ci.ProductID
            LEFT JOIN dbo.Consignment c ON c.ID = ci.ConsignmentID
            LEFT JOIN dbo.ProductIncome pi ON pi.ID = c.ProductIncomeID
            WHERE ci.Deleted = 0
                  AND ci.RemainingQty > 0
                  AND ci.AccountingPrice > 0
                  AND (
                      pi.ID IS NULL
                      OR pi.SourceDocumentType IS NULL
                      OR pi.SourceDocumentType <> :debt_doc_type
                  )
            GROUP BY ci.ProductID
        ),
        UnitCosts AS (
            SELECT product_id,
                   CAST(
                       cost_value_eur / NULLIF(cost_qty, 0)
                       AS decimal(28, 8)
                   ) AS unit_cost_eur
            FROM CostLots
        )
        SELECT stock.product_id,
               stock.qty_on_hand,
               cost.unit_cost_eur,
               CASE
                   WHEN cost.unit_cost_eur IS NULL THEN NULL
                   ELSE CAST(
                       stock.qty_on_hand * cost.unit_cost_eur
                       AS decimal(38, 8)
                   )
               END AS eur_value
        FROM OperationalStock stock
        LEFT JOIN UnitCosts cost ON cost.product_id = stock.product_id
        """,
        params,
    )


def _stock_readiness_reason(metrics: dict[str, Any]) -> str | None:
    """Why operational inventory cannot support truthful product analytics."""
    global_rows = int(metrics.get("global_availability_row_count") or 0)
    global_qty = exact.decimal_value(
        metrics.get("global_available_qty") or 0,
        "global_available_qty",
        non_negative=True,
    )
    if global_rows <= 0:
        return "product_availability_missing"
    if global_qty > 0 and int(metrics.get("role_marked_storage_count") or 0) <= 0:
        return "storage_roles_missing"
    if global_qty > 0 and int(metrics.get("sellable_availability_row_count") or 0) <= 0:
        return "sellable_inventory_missing"
    if global_qty > 0 and exact.decimal_value(
        metrics.get("sellable_available_qty") or 0,
        "sellable_available_qty",
        non_negative=True,
    ) <= 0:
        return "sellable_inventory_empty"
    return None


def stock_source_readiness() -> dict[str, Any]:
    """Small business-readiness snapshot over the same ProductAvailability storage scope."""
    rows = query(
        f"""
        SELECT
            (
                SELECT COUNT_BIG(*)
                FROM dbo.ProductAvailability pa
                JOIN dbo.Storage s ON s.ID = pa.StorageID
                WHERE pa.Deleted = 0 AND s.Deleted = 0 AND s.ForDefective = 0
            ) AS global_availability_row_count,
            (
                SELECT COUNT_BIG(DISTINCT pa.ProductID)
                FROM dbo.ProductAvailability pa
                JOIN dbo.Storage s ON s.ID = pa.StorageID
                WHERE pa.Deleted = 0 AND s.Deleted = 0 AND s.ForDefective = 0
            ) AS global_product_count,
            (
                SELECT ISNULL(SUM({_PA_QTY}), CAST(0 AS decimal(38, 8)))
                FROM dbo.ProductAvailability pa
                JOIN dbo.Storage s ON s.ID = pa.StorageID
                WHERE pa.Deleted = 0 AND s.Deleted = 0 AND s.ForDefective = 0
            ) AS global_available_qty,
            (
                SELECT COUNT_BIG(*)
                FROM dbo.Storage s
                WHERE {_SELLABLE_STORAGE}
            ) AS role_marked_storage_count,
            (
                SELECT COUNT_BIG(*)
                FROM dbo.ProductAvailability pa
                JOIN dbo.Storage s ON s.ID = pa.StorageID
                WHERE pa.Deleted = 0 AND {_SELLABLE_STORAGE}
            ) AS sellable_availability_row_count,
            (
                SELECT COUNT_BIG(DISTINCT pa.ProductID)
                FROM dbo.ProductAvailability pa
                JOIN dbo.Storage s ON s.ID = pa.StorageID
                WHERE pa.Deleted = 0 AND {_SELLABLE_STORAGE}
            ) AS sellable_product_count,
            (
                SELECT ISNULL(SUM({_PA_QTY}), CAST(0 AS decimal(38, 8)))
                FROM dbo.ProductAvailability pa
                JOIN dbo.Storage s ON s.ID = pa.StorageID
                WHERE pa.Deleted = 0 AND {_SELLABLE_STORAGE}
            ) AS sellable_available_qty
        """
    )
    row = rows[0] if rows else {}
    snapshot = {
        "global_availability_row_count": int(row.get("global_availability_row_count") or 0),
        "global_product_count": int(row.get("global_product_count") or 0),
        "global_available_qty": exact.quantity(
            row.get("global_available_qty") or 0,
            "global_available_qty",
        ),
        "role_marked_storage_count": int(row.get("role_marked_storage_count") or 0),
        "sellable_availability_row_count": int(
            row.get("sellable_availability_row_count") or 0
        ),
        "sellable_product_count": int(row.get("sellable_product_count") or 0),
        "sellable_available_qty": exact.quantity(
            row.get("sellable_available_qty") or 0,
            "sellable_available_qty",
        ),
    }
    reason = _stock_readiness_reason(snapshot)
    snapshot.update({"ready": reason is None, "reason": reason})
    return snapshot


def _confirmed_query(sql: str, params: dict) -> list[dict]:
    return canonical.validate_rows(query(sql, params))


def sold_product_ids(as_of: str, window_days: int) -> set[int]:
    """Products with confirmed invoice demand in the exact Kyiv calendar interval."""
    _, params = _day_history_params(as_of, window_days)
    rows = _confirmed_query(canonical.facts_cte() + """
        SELECT product_id, SUM(arithmetic_failures) AS arithmetic_failures
        FROM salesFacts GROUP BY product_id
        """, params)
    return {exact.positive_int(row["product_id"], "confirmed product_id") for row in rows}


def sales_velocity(as_of: str, window_days: int,
                   product_ids: Sequence[int] | None = None) -> list[dict]:
    """Confirmed gross demand, invoice count and recency; returns never become negative demand."""
    _, params = _day_history_params(as_of, window_days)
    flt = _in_filter("product_id", "p", product_ids, params)
    return _confirmed_query(canonical.facts_cte() + f"""
        SELECT product_id, SUM(sold_qty) AS sold_qty,
               COUNT(DISTINCT sale_id) AS order_count,
               MAX(event_local) AS last_sale, MIN(event_local) AS first_sale,
               DATEDIFF(day, MAX(event_local), CAST(CAST(:asof AS datetime2) AT TIME ZONE 'UTC' AT TIME ZONE 'FLE Standard Time' AS datetime2)) AS days_since_last,
               SUM(arithmetic_failures) AS arithmetic_failures
        FROM salesFacts WHERE 1=1{flt}
        GROUP BY product_id
        """, params)


def avg_sale_price_eur(as_of: str, window_days: int,
                       product_ids: Sequence[int] | None = None) -> list[dict]:
    """Quantity-weighted confirmed gross price; zero-price lines remain in the population."""
    _, params = _day_history_params(as_of, window_days)
    flt = _in_filter("product_id", "p", product_ids, params)
    return _confirmed_query(canonical.facts_cte() + f"""
        SELECT product_id, SUM(revenue_eur) AS revenue_eur, SUM(sold_qty) AS sold_qty,
               CAST(SUM(revenue_eur) / NULLIF(SUM(sold_qty), 0) AS decimal(28,8)) AS avg_price_eur,
               SUM(arithmetic_failures) AS arithmetic_failures
        FROM salesFacts WHERE 1=1{flt}
        GROUP BY product_id
        """, params)


def returns_for_products(as_of: str, window_days: int,
                         product_ids: Sequence[int] | None = None) -> list[dict]:
    """Active return Qty and actual EUR Amount, on FromDate, with complete native currency proof."""
    _, params = _day_history_params(as_of, window_days)
    flt = _in_filter("product_id", "p", product_ids, params)
    rows = _confirmed_query(canonical.facts_cte(returns=True) + f"""
        SELECT product_id, SUM(returned_qty) AS returned_qty, COUNT_BIG(*) AS return_lines,
               SUM(known_returned_value_eur) AS known_returned_value_eur,
               CASE WHEN SUM(unresolved_amount_count)=0 THEN SUM(known_returned_value_eur) END AS returned_value_eur,
               SUM(unresolved_amount_count) AS unresolved_amount_count,
               SUM(money_returned_lines) AS money_returned_lines,
               SUM(arithmetic_failures) AS arithmetic_failures,
               SUM(ambiguous_lines) AS ambiguous_lines
        FROM returnFacts WHERE 1=1{flt}
        GROUP BY product_id
        """, params)
    return rows


def monthly_units(as_of: str, months: int,
                  product_ids: Sequence[int] | None = None) -> list[dict]:
    """Confirmed gross demand in Kyiv calendar months; zero buckets are filled by the caller."""
    _, params = _month_history_params(as_of, months)
    flt = _in_filter("product_id", "p", product_ids, params)
    return _confirmed_query(canonical.facts_cte() + f"""
        SELECT product_id, CONVERT(char(7), event_local, 126) AS ym,
               SUM(sold_qty) AS units, SUM(arithmetic_failures) AS arithmetic_failures
        FROM salesFacts WHERE 1=1{flt}
        GROUP BY product_id, CONVERT(char(7), event_local, 126)
        """, params)


def monthly_product_sales(product_id: int, window_start: str, as_of: str) -> list[dict]:
    """Gross and signed net amounts share one canonical fact population and Kyiv month grid."""
    _, params = _explicit_history_params(as_of, window_start)
    params["product_id"] = exact.positive_int(product_id, "product_id")
    return canonical.financial_rows(query(canonical.facts_cte(returns=True) + f"""
        SELECT CONVERT(char(7), event_local, 126) AS ym,
               SUM(sold_qty) AS units, SUM(returned_qty) AS returned_units,
               COUNT(DISTINCT CASE WHEN is_return=0 THEN sale_id END) AS order_count,
               {canonical.FINANCIAL_SELECT}
        FROM financialFacts WHERE product_id = :product_id
        GROUP BY CONVERT(char(7), event_local, 126)
        ORDER BY ym
        """, params))


def _region_filter(region_id: int | None, params: dict) -> str:
    if region_id == 0:
        return " AND region_id IS NULL"
    if region_id is not None:
        params["region_id"] = exact.positive_int(region_id, "region_id")
        return " AND region_id = :region_id"
    return ""


def regional_product_sales(as_of: str, window_days: int, product_ids: Sequence[int] | None = None,
                           region_id: int | None = None) -> list[dict]:
    """Current client region over canonical gross/net facts; null region is retained once."""
    _, params = _day_history_params(as_of, window_days)
    flt = _in_filter("product_id", "p", product_ids, params) + _region_filter(region_id, params)
    rows = canonical.financial_rows(query(canonical.facts_cte(returns=True) + f"""
        SELECT product_id, region_id, MAX(region_name) AS region_name,
               SUM(sold_qty) AS regional_units, SUM(returned_qty) AS returned_units,
               COUNT(DISTINCT CASE WHEN is_return=0 THEN sale_id END) AS regional_order_count,
               COUNT(DISTINCT client_id) AS regional_client_count,
               {canonical.FINANCIAL_SELECT}
        FROM financialFacts WHERE 1=1{flt}
        GROUP BY product_id, region_id
        """, params))
    return [{**row, "regional_revenue_eur": row["revenue_eur"],
             "regional_net_revenue_eur": row["net_revenue_eur"],
             "regional_known_net_revenue_eur": row["known_net_revenue_eur"]} for row in rows]


def regional_demand_summary(as_of: str, window_days: int) -> list[dict]:
    """All current client regions, including unassigned, over one gross/net population."""
    _, params = _day_history_params(as_of, window_days)
    return canonical.financial_rows(query(canonical.facts_cte(returns=True) + f"""
        SELECT region_id, MAX(region_name) AS region_name,
               COUNT(DISTINCT client_id) AS client_count,
               COUNT(DISTINCT CASE WHEN is_return=0 THEN sale_id END) AS order_count,
               COUNT(DISTINCT product_id) AS product_count,
               SUM(sold_qty) AS units, SUM(returned_qty) AS returned_units,
               {canonical.FINANCIAL_SELECT}
        FROM financialFacts GROUP BY region_id ORDER BY revenue_eur DESC
        """, params))


def product_meta(product_ids: Sequence[int], as_of: str) -> dict[int, dict]:
    """Product display metadata plus the latest factual producer in ``[floor, as_of)``."""
    floor = get_settings().source_history_start_date
    source_window = resolve_history_window(as_of, floor, floor)
    out: dict[int, dict] = {}
    ids = [int(x) for x in product_ids]
    for i in range(0, len(ids), 1000):
        chunk = ids[i:i + 1000]
        ph, params = in_clause("p", chunk)
        params.update(
            {
                "asof": kyiv_midnight_utc(as_of),
                "source_history_start": kyiv_midnight_utc(source_window.source_history_start),
            }
        )
        rows = query(
            f"""
            WITH factual_supply AS (
                SELECT sioi.ProductID AS product_id,
                       so.ClientID AS producer_id,
                       si.DateFrom AS source_date,
                       si.ID AS source_document_id,
                       sioi.ID AS source_line_id
                FROM dbo.SupplyInvoice si
                JOIN dbo.SupplyInvoiceOrderItem sioi
                     ON sioi.SupplyInvoiceID = si.ID AND sioi.Deleted = 0
                JOIN dbo.SupplyOrder so ON so.ID = si.SupplyOrderID
                WHERE si.Deleted = 0
                      AND so.ClientID IS NOT NULL
                      AND si.DateFrom >= :source_history_start
                      AND si.DateFrom < :asof
                      AND sioi.ProductID IN {ph}

                UNION ALL

                SELECT soui.ProductID AS product_id,
                       COALESCE(soui.SupplierID, sou.SupplierID) AS producer_id,
                       sou.FromDate AS source_date,
                       sou.ID AS source_document_id,
                       soui.ID AS source_line_id
                FROM dbo.SupplyOrderUkraine sou
                JOIN dbo.SupplyOrderUkraineItem soui
                     ON soui.SupplyOrderUkraineID = sou.ID AND soui.Deleted = 0
                WHERE sou.Deleted = 0
                      AND NOT (sou.IsFromCockpit = 1 AND sou.IsPlaced = 0)
                      AND COALESCE(soui.SupplierID, sou.SupplierID) IS NOT NULL
                      AND sou.FromDate >= :source_history_start
                      AND sou.FromDate < :asof
                      AND soui.ProductID IN {ph}
            ),
            latest_producer AS (
                SELECT product_id, producer_id
                FROM (
                    SELECT product_id,
                           producer_id,
                           ROW_NUMBER() OVER (
                               PARTITION BY product_id
                               ORDER BY source_date DESC, source_document_id DESC, source_line_id DESC
                           ) AS rn
                    FROM factual_supply
                ) ranked
                WHERE rn = 1
            )
            SELECT p.ID AS product_id, p.Name AS name, p.VendorCode AS vendor_code,
                   p.HasAnalogue AS has_analogue, p.IsForSale AS is_for_sale,
                   lp.producer_id AS primary_producer_id,
                   COALESCE(NULLIF(producer.SupplierName, ''),
                            NULLIF(producer.FullName, ''),
                            NULLIF(producer.Name, ''),
                            CONVERT(nvarchar(32), lp.producer_id)) AS primary_producer_name
            FROM dbo.Product p
            LEFT JOIN latest_producer lp ON lp.product_id = p.ID
            LEFT JOIN dbo.Client producer ON producer.ID = lp.producer_id
            WHERE p.ID IN {ph}
            """,
            params,
        )
        for r in rows:
            out[int(r["product_id"])] = r
    return out
