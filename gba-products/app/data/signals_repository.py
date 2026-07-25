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
  - SALE-side OrderItem.PricePerItem is ALREADY EUR — never wrap/convert it. Qty/Amount columns
    are SQL ``float`` in the legacy schema, so every quantity and money aggregate casts operands
    to Decimal BEFORE SUM/multiplication. Multiplying float * decimal promotes the expression to
    binary float and can move an accounting half-cent.
  - Time windows MUST use Order.Created. OrderItem.Created is truncated (~3 days) and is unusable.
  - SALE VALIDITY (Sale/Order/OrderItem spine) = oi.IsValidForCurrentSale = 1, NOT Deleted = 0.
    In this 1С-synced DB dbo.[Order]/OrderItem are ~80%/84% Deleted=1 (the sync flips Deleted on
    every superseded/revision row), so o.Deleted=0 AND oi.Deleted=0 silently keeps only ~16% of real
    sale lines and undercounts every sales-based signal ~3.5x. IsValidForCurrentSale is the canonical
    "this line is the live sale" flag (only 231 invalid lines DB-wide). This applies ONLY to the
    Sale/Order/OrderItem spine — returns (SaleReturn/SaleReturnItem) and side tables keep Deleted=0.
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
_SALE_QTY = "CAST(oi.Qty AS decimal(18, 8))"
_SALE_PRICE = "CAST(oi.PricePerItem AS decimal(28, 14))"
_SALE_AMOUNT = f"{_SALE_QTY} * {_SALE_PRICE}"
_RETURN_QTY = "CAST(sri.Qty AS decimal(18, 8))"

_SALES_HISTORY_WINDOW = (
    "o.Created >= :source_history_start "
    "AND o.Created >= :history_start "
    "AND o.Created < :asof"
)
_RETURNS_HISTORY_WINDOW = (
    "sr.FromDate >= :source_history_start "
    "AND sr.FromDate >= :history_start "
    "AND sr.FromDate < :asof"
)

# The synthetic "Ввід боргів" (debt-entry) product carries accounting noise — it is a 1С
# debt-injection line, not a real sale or return (it ranks #1 by revenue, ~€7.4M). Every
# sales-spine aggregate (velocity / avg sale price / monthly units) and the returns query must
# exclude it; sold_product_ids is only a membership set (never aggregated) so it is left as-is.
# The ID is NOT stable (the 1С sync re-mints the Product row), so it is resolved from the live
# table at startup and re-resolved hourly; settings.synthetic_product_id (env) overrides when
# set, and the last known live row is the offline fallback.
_SYNTHETIC_FALLBACK_ID = 29555414
_SYNTHETIC_REFRESH_SECONDS = 3600
_synthetic_cached: tuple[int, float] | None = None


def _window_params(as_of: str, window: HistoryWindow) -> dict[str, Any]:
    return {
        "asof": as_of,
        "source_history_start": window.source_history_start.isoformat(),
        "history_start": window.effective_start.isoformat(),
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


def sold_product_ids(as_of: str, window_days: int) -> set[int]:
    """Distinct ProductIDs with at least one valid sale line in the window (Order.Created)."""
    _, params = _day_history_params(as_of, window_days)
    rows = query(
        f"""
        SELECT DISTINCT oi.ProductID AS product_id
        FROM dbo.OrderItem oi
        JOIN dbo.[Order] o ON o.ID = oi.OrderID
        WHERE oi.IsValidForCurrentSale = 1 AND oi.ProductID IS NOT NULL
              AND {_SALES_HISTORY_WINDOW}
        """,
        params,
    )
    return {int(r["product_id"]) for r in rows}


def sales_velocity(as_of: str, window_days: int,
                   product_ids: Sequence[int] | None = None) -> list[dict]:
    """Per-product sold qty / order count / recency over the window (Order.Created)."""
    _, params = _day_history_params(as_of, window_days)
    params["synth"] = synthetic_product_id()
    flt = _in_filter("oi.ProductID", "p", product_ids, params)
    return query(
        f"""
        SELECT oi.ProductID AS product_id,
               SUM({_SALE_QTY}) AS sold_qty,
               COUNT(DISTINCT o.ID) AS order_count,
               MAX(o.Created) AS last_sale,
               MIN(o.Created) AS first_sale,
               DATEDIFF(day, MAX(o.Created), :asof) AS days_since_last
        FROM dbo.OrderItem oi
        JOIN dbo.[Order] o ON o.ID = oi.OrderID
        WHERE oi.IsValidForCurrentSale = 1 AND oi.ProductID IS NOT NULL AND oi.ProductID <> :synth
              AND {_SALES_HISTORY_WINDOW}{flt}
        GROUP BY oi.ProductID
        """,
        params,
    )


def avg_sale_price_eur(as_of: str, window_days: int,
                       product_ids: Sequence[int] | None = None) -> list[dict]:
    """Per-product qty-weighted average SALE price in EUR (OrderItem.PricePerItem is already EUR)."""
    _, params = _day_history_params(as_of, window_days)
    params["synth"] = synthetic_product_id()
    flt = _in_filter("oi.ProductID", "p", product_ids, params)
    return query(
        f"""
        WITH Sales AS (
            SELECT oi.ProductID AS product_id,
                   SUM({_SALE_AMOUNT}) AS revenue_eur,
                   SUM({_SALE_QTY}) AS sold_qty
            FROM dbo.OrderItem oi
            JOIN dbo.[Order] o ON o.ID = oi.OrderID
            WHERE oi.IsValidForCurrentSale = 1
                  AND oi.ProductID IS NOT NULL
                  AND oi.ProductID <> :synth
                  AND oi.PricePerItem > 0
                  AND {_SALES_HISTORY_WINDOW}{flt}
            GROUP BY oi.ProductID
        )
        SELECT product_id,
               CAST(revenue_eur / NULLIF(sold_qty, 0) AS decimal(28, 8))
                   AS avg_price_eur,
               revenue_eur,
               sold_qty
        FROM Sales
        """,
        params,
    )


def returns_for_products(as_of: str, window_days: int,
                         product_ids: Sequence[int] | None = None) -> list[dict]:
    """Per-product real returned quantity over the window.

    LOAD-BEARING RETURNS RULES (verified on ConcordDb_V5, N=2.6k active return lines):
      - The return DATE is SaleReturn.FromDate. SaleReturn.Created (and SaleReturnItem.Created)
        is a bulk-sync MIRROR timestamp — it clusters on a handful of import days, so windowing on
        it silently mis-dates every return. Window on sr.FromDate.
      - Returned QUANTITY is SUM(SaleReturnItem.Qty). This is the canonical server rule:
        GetAllOrderedProductsHistory groups SaleReturnItem by OrderItemID and sums Qty, return
        reports sum Qty directly, and mutation/import paths persist Qty. Group by
        (SaleReturnID, OrderItemID) first so price/flags stay one-per-return-line; SUM within the
        group intentionally preserves partial quantities and multiple active rows. MAX(OrderItem.Qty)
        is not a fallback: it destroys partial returns and replaces duplicate-row quantities with
        the original sold quantity.
      - oi.Deleted is NOT filtered: processing a return marks the original sale line Deleted=1, so
        ~73% of returned lines are "deleted" — filtering them drops most real returns.
      - Active returns only: sr.Deleted = 0 AND sr.IsCanceled = 0. Exclude the synthetic debt-entry
        product. oi.PricePerItem is already EUR (no conversion).
    """
    _, params = _day_history_params(as_of, window_days)
    params["synth"] = synthetic_product_id()
    flt = _in_filter("oi.ProductID", "p", product_ids, params)
    return query(
        f"""
        SELECT product_id,
               SUM(returned_qty) AS returned_qty,
               COUNT(*) AS return_lines,
               CAST(
                   SUM(returned_qty * price_eur)
                   AS decimal(38, 8)
               ) AS returned_value_eur,
               SUM(money_returned) AS money_returned_lines
        FROM (
            SELECT oi.ProductID AS product_id,
                   sri.SaleReturnID,
                   sri.OrderItemID,
                   SUM({_RETURN_QTY}) AS returned_qty,
                   MAX({_SALE_PRICE}) AS price_eur,
                   MAX(CASE WHEN sri.IsMoneyReturned = 1 THEN 1 ELSE 0 END) AS money_returned
            FROM dbo.SaleReturnItem sri
            JOIN dbo.OrderItem oi ON oi.ID = sri.OrderItemID
            JOIN dbo.SaleReturn sr ON sr.ID = sri.SaleReturnID
                 AND sr.Deleted = 0 AND sr.IsCanceled = 0
            WHERE sri.Deleted = 0 AND oi.ProductID IS NOT NULL AND oi.ProductID <> :synth
                  AND {_RETURNS_HISTORY_WINDOW}{flt}
            GROUP BY oi.ProductID, sri.SaleReturnID, sri.OrderItemID
        ) line
        GROUP BY product_id
        """,
        params,
    )


def monthly_units(as_of: str, months: int,
                  product_ids: Sequence[int] | None = None) -> list[dict]:
    """Per-product per-month units sold over the trailing window (Order.Created) — feeds XYZ CV,
    trend and lifecycle. Months with no sales are absent; the caller fills the grid with zeros."""
    _, params = _month_history_params(as_of, months)
    params["synth"] = synthetic_product_id()
    flt = _in_filter("oi.ProductID", "p", product_ids, params)
    return query(
        f"""
        SELECT oi.ProductID AS product_id,
               CONVERT(char(7), o.Created, 126) AS ym,
               SUM({_SALE_QTY}) AS units
        FROM dbo.OrderItem oi
        JOIN dbo.[Order] o ON o.ID = oi.OrderID
        WHERE oi.IsValidForCurrentSale = 1 AND oi.ProductID IS NOT NULL AND oi.ProductID <> :synth
              AND {_SALES_HISTORY_WINDOW}{flt}
        GROUP BY oi.ProductID, CONVERT(char(7), o.Created, 126)
        """,
        params,
    )


def monthly_product_sales(product_id: int, window_start: str, as_of: str) -> list[dict]:
    """Calendar-month actual sales for one product over ``[window_start, as_of)``.

    This intentionally repeats the canonical sales-spine rules used by the portfolio signals:
    ``Order.Created`` is the business date, ``IsValidForCurrentSale`` selects the live line, and
    ``OrderItem.PricePerItem`` is already EUR. The caller owns the dense month grid because SQL only
    returns months with sales.
    """
    _, params = _explicit_history_params(as_of, window_start)
    params.update(
        {
            "product_id": int(product_id),
            "synth": synthetic_product_id(),
        }
    )
    return query(
        f"""
        WITH MonthlySales AS (
            SELECT CONVERT(char(7), o.Created, 126) AS ym,
                   SUM({_SALE_QTY}) AS units,
                   COUNT(DISTINCT o.ID) AS order_count,
                   SUM({_SALE_AMOUNT}) AS revenue_eur
            FROM dbo.OrderItem oi
            JOIN dbo.[Order] o ON o.ID = oi.OrderID
            WHERE oi.IsValidForCurrentSale = 1
                  AND oi.ProductID = :product_id
                  AND oi.ProductID <> :synth
                  AND {_SALES_HISTORY_WINDOW}
            GROUP BY CONVERT(char(7), o.Created, 126)
        )
        SELECT ym,
               units,
               order_count,
               revenue_eur,
               CAST(revenue_eur / NULLIF(units, 0) AS decimal(28, 8))
                   AS avg_price_eur
        FROM MonthlySales
        ORDER BY ym
        """,
        params,
    )


def regional_product_sales(as_of: str, window_days: int, product_ids: Sequence[int] | None = None,
                           region_id: int | None = None) -> list[dict]:
    """Per-product regional demand over the window.

    Region is the oblast-level natural key `Client.RegionID`, reached through the sale's
    ClientAgreement (`Order.ClientAgreementID -> ClientAgreement.ClientID -> Client.RegionID`).
    Do NOT use `RegionCodeID`: it is per-client address/code granularity and does not group demand.
    """
    _, params = _day_history_params(as_of, window_days)
    params["synth"] = synthetic_product_id()
    flt = _in_filter("oi.ProductID", "p", product_ids, params)
    region_filter = ""
    if region_id is not None:
        params["region_id"] = int(region_id)
        region_filter = " AND c.RegionID = :region_id"
    return query(
        f"""
        SELECT oi.ProductID AS product_id,
               c.RegionID AS region_id,
               MAX(r.Name) AS region_name,
               SUM({_SALE_QTY}) AS regional_units,
               SUM({_SALE_AMOUNT}) AS regional_revenue_eur,
               COUNT(DISTINCT o.ID) AS regional_order_count,
               COUNT(DISTINCT ca.ClientID) AS regional_client_count
        FROM dbo.OrderItem oi
        JOIN dbo.[Order] o ON o.ID = oi.OrderID
        JOIN dbo.ClientAgreement ca ON ca.ID = o.ClientAgreementID
        JOIN dbo.Client c ON c.ID = ca.ClientID
        LEFT JOIN dbo.Region r ON r.ID = c.RegionID
        WHERE oi.IsValidForCurrentSale = 1 AND oi.ProductID IS NOT NULL AND oi.ProductID <> :synth
              AND c.RegionID IS NOT NULL
              AND {_SALES_HISTORY_WINDOW}{flt}{region_filter}
        GROUP BY oi.ProductID, c.RegionID
        """,
        params,
    )


def regional_demand_summary(as_of: str, window_days: int) -> list[dict]:
    """Portfolio demand summary by Client.RegionID over the sales window."""
    _, params = _day_history_params(as_of, window_days)
    params["synth"] = synthetic_product_id()
    return query(
        f"""
        SELECT c.RegionID AS region_id,
               MAX(r.Name) AS region_name,
               COUNT(DISTINCT ca.ClientID) AS client_count,
               COUNT(DISTINCT o.ID) AS order_count,
               COUNT(DISTINCT oi.ProductID) AS product_count,
               SUM({_SALE_QTY}) AS units,
               SUM({_SALE_AMOUNT}) AS revenue_eur
        FROM dbo.OrderItem oi
        JOIN dbo.[Order] o ON o.ID = oi.OrderID
        JOIN dbo.ClientAgreement ca ON ca.ID = o.ClientAgreementID
        JOIN dbo.Client c ON c.ID = ca.ClientID
        LEFT JOIN dbo.Region r ON r.ID = c.RegionID
        WHERE oi.IsValidForCurrentSale = 1 AND oi.ProductID IS NOT NULL AND oi.ProductID <> :synth
              AND c.RegionID IS NOT NULL
              AND {_SALES_HISTORY_WINDOW}
        GROUP BY c.RegionID
        ORDER BY revenue_eur DESC
        """,
        params,
    )


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
                "asof": as_of,
                "source_history_start": source_window.source_history_start.isoformat(),
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
