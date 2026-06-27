"""Read-only product-intelligence signals over ConcordDb_V5. All parameterized.

LOAD-BEARING DATA RULES (verified on ConcordDb_V5):
  - On-hand sellable stock = ReSaleAvailability(Deleted=0, RemainingQty>0) joined via ConsignmentItem
    (authoritative ProductID) + ProductAvailability (StorageID) -> Storage. RSA is the cost-layer grain;
    sum RemainingQty directly. Do NOT also join OrderItem/Transfer/Reservation link cols (row multiply).
  - EUR unit cost = ConsignmentItem.Price directly (ci.Price IS ALREADY EUR — it equals
    ci.AccountingPrice on every on-hand lot; gba-pricing's unit_cost_eur uses AccountingPrice with
    no conversion). RSA.PricePerItem is the UAH amount (= ci.Price * ExchangeRate); dividing ci.Price
    by ExchangeRate (or reading PricePerItem raw) mis-scales the EUR value ~50x. No CurrencyID here.
  - 1С DEBT-IMPORT lot contamination (verified live; mirrors gba-pricing unit_cost_eur exactly):
    a lot whose dbo.ProductIncome.SourceDocumentType = 1 (the 1С debt/balance-import document) carries
    an inflated debt-injection AccountingPrice (~55x the real cost) on BOTH Consignment.IsImportedFromOneC
    and IsVirtual lots — neither Consignment flag isolates it. These lots inflated the on-hand EUR value
    ~3x (€985k contaminated vs €323k real; 67.2% / €662k of the total came from srcDoc=1 lots, touching
    357 products) and overstated per-product unit_cost up to 11.5x (pid 26157549: 1.0406 -> 0.0938).
    So we JOIN ConsignmentItem -> Consignment (ci.ConsignmentID) -> ProductIncome (c.ProductIncomeID) and
    EXCLUDE SourceDocumentType=1 from BOTH the qty/value and the derived cost (a lot with no ProductIncome
    — e.g. a pure transfer — is kept via pi.ID IS NULL). Real supply lots are SourceDocumentType IN (2,3).
    gba-pricing (pricing_repository.unit_cost_eur) and gba-procure deliberately exclude these too.
  - Sellable warehouses: Storage.Deleted=0 AND ForDefective=0 AND (AvailableForReSale=1 OR IsResale=1).
  - SALE-side OrderItem.PricePerItem is ALREADY EUR — never wrap/convert it.
  - Time windows MUST use Order.Created. OrderItem.Created is truncated (~3 days) and is unusable.
  - SALE VALIDITY (Sale/Order/OrderItem spine) = oi.IsValidForCurrentSale = 1, NOT Deleted = 0.
    In this 1С-synced DB dbo.[Order]/OrderItem are ~80%/84% Deleted=1 (the sync flips Deleted on
    every superseded/revision row), so o.Deleted=0 AND oi.Deleted=0 silently keeps only ~16% of real
    sale lines and undercounts every sales-based signal ~3.5x. IsValidForCurrentSale is the canonical
    "this line is the live sale" flag (only 231 invalid lines DB-wide). This applies ONLY to the
    Sale/Order/OrderItem spine — returns (SaleReturn/SaleReturnItem) and side tables keep Deleted=0.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from app.data.db import in_clause, query

# 1С debt/balance-import document type on dbo.ProductIncome.SourceDocumentType. Such lots carry an
# inflated balance-import AccountingPrice (== ci.Price) across BOTH Consignment.IsImportedFromOneC and
# IsVirtual; they are EXCLUDED from on-hand qty/value and the derived unit cost so the inventory EUR
# value and per-product cost/margin are not contaminated. Mirrors gba-pricing unit_cost_eur exactly.
# Real supply lots are SourceDocumentType IN (2,3).
_DEBT_IMPORT_SOURCE_DOCUMENT_TYPE = 1

_STOCK_FROM = """
    FROM dbo.ReSaleAvailability    rsa
    JOIN dbo.ConsignmentItem       ci ON ci.ID = rsa.ConsignmentItemID
    JOIN dbo.ProductAvailability   pa ON pa.ID = rsa.ProductAvailabilityID
    JOIN dbo.Storage               s  ON s.ID  = pa.StorageID
    LEFT JOIN dbo.Consignment      c  ON c.ID  = ci.ConsignmentID
    LEFT JOIN dbo.ProductIncome    pi ON pi.ID = c.ProductIncomeID
    WHERE rsa.Deleted = 0 AND rsa.RemainingQty > 0
          AND s.Deleted = 0 AND s.ForDefective = 0
          AND (s.AvailableForReSale = 1 OR s.IsResale = 1)
          AND (pi.ID IS NULL OR pi.SourceDocumentType <> :debt_doc_type)
"""

# The synthetic "Ввід боргів" (debt-entry) product carries accounting noise — it is a 1С
# debt-injection line, not a real sale or return (it ranks #1 by revenue, ~€7.4M). Every
# sales-spine aggregate (velocity / avg sale price / monthly units) and the returns query must
# exclude it; sold_product_ids is only a membership set (never aggregated) so it is left as-is.
_SYNTHETIC_PRODUCT_ID = 25422404


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
    """Per-product current on-hand sellable stock + EUR value (the canonical inventory query).

    Both qty and EUR value EXCLUDE 1С debt-import lots (ProductIncome.SourceDocumentType=1, see
    _STOCK_FROM): their inflated AccountingPrice (== ci.Price) otherwise ~3x's the inventory EUR
    value and overstates the unit_cost the margin layer derives (eur_value/qty) up to 11.5x.
    """
    params: dict[str, Any] = {"debt_doc_type": _DEBT_IMPORT_SOURCE_DOCUMENT_TYPE}
    flt = _in_filter("ci.ProductID", "p", product_ids, params)
    return query(
        f"""
        SELECT ci.ProductID AS product_id,
               SUM(rsa.RemainingQty) AS qty_on_hand,
               SUM(rsa.RemainingQty * ci.Price) AS eur_value
        {_STOCK_FROM}{flt}
        GROUP BY ci.ProductID
        """,
        params,
    )


def sold_product_ids(as_of: str, window_days: int) -> set[int]:
    """Distinct ProductIDs with at least one valid sale line in the window (Order.Created)."""
    rows = query(
        """
        SELECT DISTINCT oi.ProductID AS product_id
        FROM dbo.OrderItem oi
        JOIN dbo.[Order] o ON o.ID = oi.OrderID
        WHERE oi.IsValidForCurrentSale = 1 AND oi.ProductID IS NOT NULL
              AND o.Created >= DATEADD(day, -:win, :asof) AND o.Created < :asof
        """,
        {"asof": as_of, "win": window_days},
    )
    return {int(r["product_id"]) for r in rows}


def sales_velocity(as_of: str, window_days: int,
                   product_ids: Sequence[int] | None = None) -> list[dict]:
    """Per-product sold qty / order count / recency over the window (Order.Created)."""
    params: dict[str, Any] = {"asof": as_of, "win": window_days, "synth": _SYNTHETIC_PRODUCT_ID}
    flt = _in_filter("oi.ProductID", "p", product_ids, params)
    return query(
        f"""
        SELECT oi.ProductID AS product_id,
               SUM(oi.Qty) AS sold_qty,
               COUNT(DISTINCT o.ID) AS order_count,
               MAX(o.Created) AS last_sale,
               MIN(o.Created) AS first_sale,
               DATEDIFF(day, MAX(o.Created), :asof) AS days_since_last
        FROM dbo.OrderItem oi
        JOIN dbo.[Order] o ON o.ID = oi.OrderID
        WHERE oi.IsValidForCurrentSale = 1 AND oi.ProductID IS NOT NULL AND oi.ProductID <> :synth
              AND o.Created >= DATEADD(day, -:win, :asof) AND o.Created < :asof{flt}
        GROUP BY oi.ProductID
        """,
        params,
    )


def avg_sale_price_eur(as_of: str, window_days: int,
                       product_ids: Sequence[int] | None = None) -> list[dict]:
    """Per-product qty-weighted average SALE price in EUR (OrderItem.PricePerItem is already EUR)."""
    params: dict[str, Any] = {"asof": as_of, "win": window_days, "synth": _SYNTHETIC_PRODUCT_ID}
    flt = _in_filter("oi.ProductID", "p", product_ids, params)
    return query(
        f"""
        SELECT oi.ProductID AS product_id,
               SUM(oi.Qty * oi.PricePerItem) / NULLIF(SUM(oi.Qty), 0) AS avg_price_eur,
               SUM(oi.Qty) AS sold_qty
        FROM dbo.OrderItem oi
        JOIN dbo.[Order] o ON o.ID = oi.OrderID
        WHERE oi.IsValidForCurrentSale = 1 AND oi.ProductID IS NOT NULL AND oi.ProductID <> :synth
              AND oi.PricePerItem > 0
              AND o.Created >= DATEADD(day, -:win, :asof) AND o.Created < :asof{flt}
        GROUP BY oi.ProductID
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
      - Returned QUANTITY. The CANONICAL source (per gba-server) is SUM(SaleReturnItem.Qty) per
        OrderItem; SaleReturn.TotalCount is computed at runtime as exactly that sum. But the 1С
        DataSync write path omits Qty (and never increments OrderItem.ReturnedQty / SaleReturnItem
        .Amount), so all three are 0 here — reading any of them makes the returns factor a dead
        constant. The faithful fallback for the same intent (sum of per-line returned quantity): each
        non-deleted SaleReturnItem is "this OrderItem line came back", so we take the line's sold
        OrderItem.Qty once per distinct (SaleReturn, OrderItem). The raw rows fragment into 1..18
        duplicate sync rows per line, so GROUP BY (return, orderitem) + MAX(Qty) collapses the noise
        (count-of-rows is wrong — it exceeds the sold qty on 386 of 522 multi-row lines).
      - oi.Deleted is NOT filtered: processing a return marks the original sale line Deleted=1, so
        ~73% of returned lines are "deleted" — filtering them drops most real returns.
      - Active returns only: sr.Deleted = 0 AND sr.IsCanceled = 0. Exclude the synthetic debt-entry
        product. oi.PricePerItem is already EUR (no conversion).
    """
    params: dict[str, Any] = {"asof": as_of, "win": window_days, "synth": _SYNTHETIC_PRODUCT_ID}
    flt = _in_filter("oi.ProductID", "p", product_ids, params)
    return query(
        f"""
        SELECT product_id,
               SUM(oi_qty) AS returned_qty,
               COUNT(*) AS return_lines,
               SUM(oi_qty * price_eur) AS returned_value_eur,
               SUM(money_returned) AS money_returned_lines
        FROM (
            SELECT oi.ProductID AS product_id,
                   sri.SaleReturnID,
                   sri.OrderItemID,
                   MAX(oi.Qty) AS oi_qty,
                   MAX(oi.PricePerItem) AS price_eur,
                   MAX(CASE WHEN sri.IsMoneyReturned = 1 THEN 1 ELSE 0 END) AS money_returned
            FROM dbo.SaleReturnItem sri
            JOIN dbo.OrderItem oi ON oi.ID = sri.OrderItemID
            JOIN dbo.SaleReturn sr ON sr.ID = sri.SaleReturnID
                 AND sr.Deleted = 0 AND sr.IsCanceled = 0
            WHERE sri.Deleted = 0 AND oi.ProductID IS NOT NULL AND oi.ProductID <> :synth
                  AND sr.FromDate >= DATEADD(day, -:win, :asof) AND sr.FromDate < :asof{flt}
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
    params: dict[str, Any] = {"asof": as_of, "months": months, "synth": _SYNTHETIC_PRODUCT_ID}
    flt = _in_filter("oi.ProductID", "p", product_ids, params)
    return query(
        f"""
        SELECT oi.ProductID AS product_id,
               FORMAT(o.Created, 'yyyy-MM') AS ym,
               SUM(oi.Qty) AS units
        FROM dbo.OrderItem oi
        JOIN dbo.[Order] o ON o.ID = oi.OrderID
        WHERE oi.IsValidForCurrentSale = 1 AND oi.ProductID IS NOT NULL AND oi.ProductID <> :synth
              AND o.Created >= DATEADD(month, -:months, :asof) AND o.Created < :asof{flt}
        GROUP BY oi.ProductID, FORMAT(o.Created, 'yyyy-MM')
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
    params: dict[str, Any] = {"asof": as_of, "win": window_days, "synth": _SYNTHETIC_PRODUCT_ID}
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
               SUM(oi.Qty) AS regional_units,
               SUM(oi.Qty * oi.PricePerItem) AS regional_revenue_eur,
               COUNT(DISTINCT o.ID) AS regional_order_count,
               COUNT(DISTINCT ca.ClientID) AS regional_client_count
        FROM dbo.OrderItem oi
        JOIN dbo.[Order] o ON o.ID = oi.OrderID
        JOIN dbo.ClientAgreement ca ON ca.ID = o.ClientAgreementID
        JOIN dbo.Client c ON c.ID = ca.ClientID
        LEFT JOIN dbo.Region r ON r.ID = c.RegionID
        WHERE oi.IsValidForCurrentSale = 1 AND oi.ProductID IS NOT NULL AND oi.ProductID <> :synth
              AND c.RegionID IS NOT NULL
              AND o.Created >= DATEADD(day, -:win, :asof) AND o.Created < :asof{flt}{region_filter}
        GROUP BY oi.ProductID, c.RegionID
        """,
        params,
    )


def regional_demand_summary(as_of: str, window_days: int) -> list[dict]:
    """Portfolio demand summary by Client.RegionID over the sales window."""
    return query(
        """
        SELECT c.RegionID AS region_id,
               MAX(r.Name) AS region_name,
               COUNT(DISTINCT ca.ClientID) AS client_count,
               COUNT(DISTINCT o.ID) AS order_count,
               COUNT(DISTINCT oi.ProductID) AS product_count,
               SUM(oi.Qty) AS units,
               SUM(oi.Qty * oi.PricePerItem) AS revenue_eur
        FROM dbo.OrderItem oi
        JOIN dbo.[Order] o ON o.ID = oi.OrderID
        JOIN dbo.ClientAgreement ca ON ca.ID = o.ClientAgreementID
        JOIN dbo.Client c ON c.ID = ca.ClientID
        LEFT JOIN dbo.Region r ON r.ID = c.RegionID
        WHERE oi.IsValidForCurrentSale = 1 AND oi.ProductID IS NOT NULL AND oi.ProductID <> :synth
              AND c.RegionID IS NOT NULL
              AND o.Created >= DATEADD(day, -:win, :asof) AND o.Created < :asof
        GROUP BY c.RegionID
        ORDER BY revenue_eur DESC
        """,
        {"asof": as_of, "win": window_days, "synth": _SYNTHETIC_PRODUCT_ID},
    )


def product_meta(product_ids: Sequence[int]) -> dict[int, dict]:
    """Name / VendorCode / HasAnalogue / IsForSale per product (chunked to respect the param cap)."""
    out: dict[int, dict] = {}
    ids = [int(x) for x in product_ids]
    for i in range(0, len(ids), 1000):
        chunk = ids[i:i + 1000]
        ph, params = in_clause("p", chunk)
        rows = query(
            f"""
            SELECT ID AS product_id, Name AS name, VendorCode AS vendor_code,
                   HasAnalogue AS has_analogue, IsForSale AS is_for_sale
            FROM dbo.Product WHERE Deleted = 0 AND ID IN {ph}
            """,
            params,
        )
        for r in rows:
            out[int(r["product_id"])] = r
    return out
