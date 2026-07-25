"""Parameterized read queries over the procurement spine.

Verified columns (ConcordDb_V5):
  SupplyOrder(OrganizationID, DateFrom, Created, OrderArrivedDate, IsOrderArrived, ...)
    FK_SupplyOrder_Organization_OrganizationID -> dbo.Organization(ID, Name)
    DateFrom = real placement date; Created = 1C sync timestamp (rewritten to ~now)
  SupplyOrderItem(SupplyOrderID, ProductID, Qty)  -- technical placeholder, not product facts
  Organization(ID, Name)  -- supplier names (NOT dbo.SupplyOrganization, 0 overlap)
  ProductAvailability(ProductID, StorageID, Amount)
  ProductReservation(ProductAvailabilityID, Qty)  -- links to product via ProductAvailability
  Order/OrderItem -- demand history (sales); filter oi.IsValidForCurrentSale=1
  Synthetic debt product («Ввід боргів», ID resolved dynamically — re-minted over time)
  excluded from sales/demand/supply candidates

  FACTUAL PRODUCT spine (SupplyOrderItem holds only a synthetic placeholder after rekey,
  so it CANNOT source candidates, MOQ, agreement history, lead time, or on_order):
    SupplyOrder -> SupplyInvoice(DateFrom) -> PackingList -> PackingListPackageOrderItem(Qty)
       -> SupplyInvoiceOrderItem(ProductID)            == ordered, real product
    ProductIncome(FromDate) -> ProductIncomeItem(Qty)  == received (netted)
       linked via PackingListPackageOrderItemID (intl) or SupplyOrderUkraineItemID (UA)
    SupplyOrderUkraine(FromDate) -> SupplyOrderUkraineItem(ProductID, Qty)  == UA ordered
  DateFrom/FromDate are REAL historical dates; SupplyOrder.Created is the 1C-sync stamp (~now).
"""
from __future__ import annotations

import hashlib
import json

from app.core.logging import get_logger
from app.data.db import in_clause, query
from app.data.synthetic import synthetic_product_id

log = get_logger("supply_repository")

# --- demand (sales) history ---

def product_daily_demand(product_id: int, as_of: str, history_days: int) -> list[dict]:
    """Per-day units sold for a product within the history window (for forecasting)."""
    return query(
        """
        SELECT CAST(o.Created AS date) AS d, SUM(oi.Qty) AS units
        FROM dbo.[Order] o
        JOIN dbo.OrderItem oi ON oi.OrderID = o.ID
        WHERE oi.ProductID = :pid
              AND oi.IsValidForCurrentSale = 1
              AND oi.ProductID <> :syn
              AND o.Created < :asof
              AND o.Created >= DATEADD(day, -:days, :asof)
        GROUP BY CAST(o.Created AS date)
        ORDER BY d
        """,
        {"pid": product_id, "asof": as_of, "days": history_days, "syn": synthetic_product_id()},
    )


# MSSQL caps a statement at 2100 parameters; the IN list shares the budget with
# the 2 window params (asof, days), so chunk well under the limit.
_DEMAND_IN_CHUNK = 1000


def product_daily_demand_bulk(
    product_ids: list[int], as_of: str, history_days: int
) -> dict[int, list[dict]]:
    """Per-day units sold for many products in ONE query per chunk (kills the N+1).

    Returns {product_id: [{"d": date, "units": float}, ...]} with each product's series
    ordered by day. Products with no sales in the window are absent from the result (the
    caller treats a missing key as an empty series, identical to product_daily_demand).
    Same filters as product_daily_demand: IsValidForCurrentSale=1, synthetic excluded, no o.Deleted.
    """
    out: dict[int, list[dict]] = {}
    if not product_ids:
        return out
    syn = synthetic_product_id()
    ids = [int(p) for p in product_ids if int(p) != syn]
    for start in range(0, len(ids), _DEMAND_IN_CHUNK):
        chunk = ids[start : start + _DEMAND_IN_CHUNK]
        ph, params = in_clause("p", chunk)
        rows = query(
            f"""
            SELECT oi.ProductID AS pid, CAST(o.Created AS date) AS d, SUM(oi.Qty) AS units
            FROM dbo.[Order] o
            JOIN dbo.OrderItem oi ON oi.OrderID = o.ID
            WHERE oi.ProductID IN {ph}
                  AND oi.IsValidForCurrentSale = 1
                  AND oi.ProductID <> :syn
                  AND o.Created < :asof
                  AND o.Created >= DATEADD(day, -:days, :asof)
            GROUP BY oi.ProductID, CAST(o.Created AS date)
            ORDER BY oi.ProductID, CAST(o.Created AS date)
            """,
            {"asof": as_of, "days": history_days, "syn": syn, **params},
        )
        for r in rows:
            out.setdefault(int(r["pid"]), []).append({"d": r["d"], "units": r["units"]})
    return out


# --- lead time per producer (factual invoice/order -> factual receipt) ---

def producer_lead_times(
    producer_id: int, as_of: str, min_days: int = 1, max_days: int = 120
) -> list[float]:
    """Plausible lead times from factual supply documents to their actual receipts.

    Arrived international orders are archived with SupplyOrder.Deleted=1 during the 1C
    lifecycle.  The active SupplyInvoice/SupplyInvoiceOrderItem and ProductIncome rows are
    therefore the authority; filtering the parent SupplyOrder by Deleted silently erases
    virtually all useful history.
    """
    rows = query(
        """
        WITH ReceiptHistory AS (
            SELECT so.ClientID AS producer_id,
                   si.ID AS document_id,
                   COALESCE(si.DateFrom, so.DateFrom) AS ordered_at,
                   MAX(pinc.FromDate) AS received_at
            FROM dbo.SupplyInvoice si
            JOIN dbo.SupplyOrder so ON so.ID = si.SupplyOrderID
            JOIN dbo.SupplyInvoiceOrderItem sioi
              ON sioi.SupplyInvoiceID = si.ID AND sioi.Deleted = 0
            JOIN dbo.PackingList pl
              ON pl.SupplyInvoiceID = si.ID AND pl.Deleted = 0
            JOIN dbo.PackingListPackageOrderItem plpoi
              ON plpoi.PackingListID = pl.ID
             AND plpoi.SupplyInvoiceOrderItemID = sioi.ID
             AND plpoi.Deleted = 0
            JOIN dbo.ProductIncomeItem pii
              ON pii.PackingListPackageOrderItemID = plpoi.ID AND pii.Deleted = 0
            JOIN dbo.ProductIncome pinc
              ON pinc.ID = pii.ProductIncomeID AND pinc.Deleted = 0
            WHERE si.Deleted = 0
                  AND so.ClientID IS NOT NULL
                  AND sioi.ProductID IS NOT NULL
                  AND sioi.ProductID <> :syn
            GROUP BY so.ClientID, si.ID, COALESCE(si.DateFrom, so.DateFrom)

            UNION ALL

            SELECT COALESCE(soui.SupplierID, sou.SupplierID) AS producer_id,
                   sou.ID AS document_id,
                   sou.FromDate AS ordered_at,
                   MAX(pinc.FromDate) AS received_at
            FROM dbo.SupplyOrderUkraine sou
            JOIN dbo.SupplyOrderUkraineItem soui
              ON soui.SupplyOrderUkraineID = sou.ID AND soui.Deleted = 0
            JOIN dbo.ProductIncomeItem pii
              ON pii.SupplyOrderUkraineItemID = soui.ID AND pii.Deleted = 0
            JOIN dbo.ProductIncome pinc
              ON pinc.ID = pii.ProductIncomeID AND pinc.Deleted = 0
            WHERE COALESCE(soui.SupplierID, sou.SupplierID) IS NOT NULL
                  AND sou.Deleted = 0
                  AND NOT (sou.IsFromCockpit = 1 AND sou.IsPlaced = 0)
                  AND soui.ProductID IS NOT NULL
                  AND soui.ProductID <> :syn
            GROUP BY COALESCE(soui.SupplierID, sou.SupplierID), sou.ID, sou.FromDate
        )
        SELECT DATEDIFF(day, ordered_at, received_at) AS lead_days
        FROM ReceiptHistory
        WHERE producer_id = :pid
              AND ordered_at IS NOT NULL
              AND received_at IS NOT NULL
              AND ordered_at < :asof
              AND received_at < :asof
              AND DATEDIFF(day, ordered_at, received_at) BETWEEN :lmin AND :lmax
        """,
        {
            "pid": producer_id,
            "asof": as_of,
            "lmin": min_days,
            "lmax": max_days,
            "syn": synthetic_product_id(),
        },
    )
    leads = [float(r["lead_days"]) for r in rows]
    log.info("producer_lead_times", producer_id=producer_id, sample_count=len(leads))
    return leads


def producer_agreement_currency(producer_id: int) -> int | None:
    """Modal currency over factual international/UA supply documents (geography proxy)."""
    rows = query(
        """
        WITH FactualAgreement AS (
            SELECT DISTINCT si.ID AS document_id,
                   so.ClientID AS producer_id,
                   a.CurrencyID AS ccy
            FROM dbo.SupplyInvoice si
            JOIN dbo.SupplyOrder so ON so.ID = si.SupplyOrderID
            JOIN dbo.ClientAgreement ca ON ca.ID = so.ClientAgreementID
            JOIN dbo.Agreement a ON a.ID = ca.AgreementID
            WHERE si.Deleted = 0
                  AND so.ClientID IS NOT NULL
                  AND a.CurrencyID IS NOT NULL
                  AND EXISTS (
                      SELECT 1
                      FROM dbo.SupplyInvoiceOrderItem sioi
                      WHERE sioi.SupplyInvoiceID = si.ID
                            AND sioi.Deleted = 0
                            AND sioi.ProductID IS NOT NULL
                            AND sioi.ProductID <> :syn
                  )

            UNION ALL

            SELECT DISTINCT sou.ID AS document_id,
                   COALESCE(soui.SupplierID, sou.SupplierID) AS producer_id,
                   a.CurrencyID AS ccy
            FROM dbo.SupplyOrderUkraine sou
            JOIN dbo.SupplyOrderUkraineItem soui
              ON soui.SupplyOrderUkraineID = sou.ID AND soui.Deleted = 0
            JOIN dbo.ClientAgreement ca ON ca.ID = sou.ClientAgreementID
            JOIN dbo.Agreement a ON a.ID = ca.AgreementID
            WHERE sou.Deleted = 0
                  AND NOT (sou.IsFromCockpit = 1 AND sou.IsPlaced = 0)
                  AND COALESCE(soui.SupplierID, sou.SupplierID) IS NOT NULL
                  AND soui.ProductID IS NOT NULL
                  AND soui.ProductID <> :syn
                  AND a.CurrencyID IS NOT NULL
        )
        SELECT TOP 1 ccy
        FROM FactualAgreement
        WHERE producer_id = :pid
        GROUP BY ccy
        ORDER BY COUNT(*) DESC, ccy
        """,
        {"pid": producer_id, "syn": synthetic_product_id()},
    )
    return int(rows[0]["ccy"]) if rows else None


def producer_name(producer_id: int) -> str | None:
    rows = query(
        "SELECT SupplierName FROM dbo.Client WHERE ID = :pid",
        {"pid": producer_id},
    )
    return rows[0]["SupplierName"] if rows else None


def producer_names(producer_ids: list[int]) -> dict[int, str]:
    if not producer_ids:
        return {}
    ph, params = in_clause("p", producer_ids)
    rows = query(
        f"SELECT ID AS pid, SupplierName AS name FROM dbo.Client WHERE ID IN {ph}",
        params,
    )
    return {int(r["pid"]): r["name"] for r in rows if r["name"]}


def product_meta(product_ids: list[int]) -> dict[int, dict]:
    """{product_id: {name, vendor_code, oe_number, image_url}} for the plan rows — so the
    console shows a readable товар (name + original number + photo) instead of a bare id.
    Chunked; primary image is the first non-deleted ProductImage."""
    meta: dict[int, dict] = {}
    for i in range(0, len(product_ids), 1000):
        chunk = product_ids[i:i + 1000]
        if not chunk:
            continue
        ph, params = in_clause("p", chunk)
        rows = query(
            f"""
            SELECT p.ID AS pid,
                   name = CASE WHEN p.NameUA IS NULL OR p.NameUA = '' THEN p.Name ELSE p.NameUA END,
                   vendor_code = p.VendorCode,
                   oe_number = p.MainOriginalNumber,
                   image_url = (
                       SELECT TOP 1 pi.ImageUrl FROM dbo.ProductImage pi
                       WHERE pi.ProductID = p.ID AND pi.Deleted = 0 AND pi.ImageUrl IS NOT NULL
                       ORDER BY pi.ID
                   )
            FROM dbo.Product p
            WHERE p.ID IN {ph}
            """,
            params,
        )
        for r in rows:
            meta[int(r["pid"])] = {
                "name": r["name"] or None,
                "vendor_code": r["vendor_code"] or None,
                "oe_number": r["oe_number"] or None,
                "image_url": r["image_url"] or None,
            }
    return meta


def products_for_producer(producer_id: int, as_of: str, history_days: int) -> list[int]:
    """Real products this producer supplied through international or UA documents."""
    rows = query(
        """
        WITH FactualSupply AS (
            SELECT so.ClientID AS producer_id,
                   sioi.ProductID AS product_id,
                   si.DateFrom AS source_date
            FROM dbo.SupplyInvoice si
            JOIN dbo.SupplyInvoiceOrderItem sioi
              ON sioi.SupplyInvoiceID = si.ID AND sioi.Deleted = 0
            JOIN dbo.SupplyOrder so ON so.ID = si.SupplyOrderID
            WHERE si.Deleted = 0
                  AND so.ClientID IS NOT NULL
                  AND sioi.ProductID IS NOT NULL
                  AND sioi.ProductID <> :syn

            UNION ALL

            SELECT COALESCE(soui.SupplierID, sou.SupplierID) AS producer_id,
                   soui.ProductID AS product_id,
                   sou.FromDate AS source_date
            FROM dbo.SupplyOrderUkraine sou
            JOIN dbo.SupplyOrderUkraineItem soui
              ON soui.SupplyOrderUkraineID = sou.ID AND soui.Deleted = 0
            WHERE sou.Deleted = 0
                  AND NOT (sou.IsFromCockpit = 1 AND sou.IsPlaced = 0)
                  AND COALESCE(soui.SupplierID, sou.SupplierID) IS NOT NULL
                  AND soui.ProductID IS NOT NULL
                  AND soui.ProductID <> :syn
        )
        SELECT DISTINCT product_id AS pid
        FROM FactualSupply
        WHERE producer_id = :pid
              AND source_date >= DATEADD(day, -:days, :asof)
              AND source_date < :asof
        """,
        {"pid": producer_id, "asof": as_of, "days": history_days,
         "syn": synthetic_product_id()},
    )
    return sorted(int(r["pid"]) for r in rows)


def derive_moq_terms(min_orders: int = 3) -> list[dict]:
    """Observed MOQ per source document; pack from Product.PackingStandard.

    Multiple invoice lines for the same product are one purchase observation, not several
    orders.  Collapse them by document first so duplicate/split lines cannot inflate sample
    counts or understate the observed MOQ.
    """
    rows = query(
        """
        WITH FactualSupplyLine AS (
            SELECT CAST(0 AS tinyint) AS source_kind,
                   si.ID AS document_id,
                   so.ClientID AS producer_id,
                   sioi.ProductID AS product_id,
                   sioi.Qty AS qty
            FROM dbo.SupplyInvoice si
            JOIN dbo.SupplyInvoiceOrderItem sioi
              ON sioi.SupplyInvoiceID = si.ID AND sioi.Deleted = 0
            JOIN dbo.SupplyOrder so ON so.ID = si.SupplyOrderID
            WHERE si.Deleted = 0
                  AND so.ClientID IS NOT NULL
                  AND sioi.ProductID IS NOT NULL
                  AND sioi.ProductID <> :syn
                  AND sioi.Qty > 0

            UNION ALL

            SELECT CAST(1 AS tinyint) AS source_kind,
                   sou.ID AS document_id,
                   COALESCE(soui.SupplierID, sou.SupplierID) AS producer_id,
                   soui.ProductID AS product_id,
                   soui.Qty AS qty
            FROM dbo.SupplyOrderUkraine sou
            JOIN dbo.SupplyOrderUkraineItem soui
              ON soui.SupplyOrderUkraineID = sou.ID AND soui.Deleted = 0
            WHERE sou.Deleted = 0
                  AND NOT (sou.IsFromCockpit = 1 AND sou.IsPlaced = 0)
                  AND COALESCE(soui.SupplierID, sou.SupplierID) IS NOT NULL
                  AND soui.ProductID IS NOT NULL
                  AND soui.ProductID <> :syn
                  AND soui.Qty > 0
        ),
        DocumentProductQty AS (
            SELECT source_kind, document_id, producer_id, product_id, SUM(qty) AS qty
            FROM FactualSupplyLine
            GROUP BY source_kind, document_id, producer_id, product_id
        )
        SELECT dpq.producer_id, dpq.product_id,
               MIN(dpq.qty) AS moq, COUNT(*) AS orders,
               TRY_CONVERT(decimal(18,3), MAX(p.PackingStandard)) AS pack
        FROM DocumentProductQty dpq
        LEFT JOIN dbo.Product p ON p.ID = dpq.product_id
        GROUP BY dpq.producer_id, dpq.product_id
        HAVING COUNT(*) >= :n
        """,
        {"n": min_orders, "syn": synthetic_product_id()},
    )
    return [
        {"producer_id": int(r["producer_id"]), "product_id": int(r["product_id"]),
         "moq": float(r["moq"]), "orders": int(r["orders"]),
         "pack": float(r["pack"]) if r["pack"] is not None else None}
        for r in rows
    ]


def all_producers(as_of: str, history_days: int) -> list[int]:
    """Producers with factual international/UA supply inside the history window."""
    rows = query(
        """
        WITH FactualSupply AS (
            SELECT so.ClientID AS producer_id,
                   sioi.ProductID AS product_id,
                   si.DateFrom AS source_date
            FROM dbo.SupplyInvoice si
            JOIN dbo.SupplyInvoiceOrderItem sioi
              ON sioi.SupplyInvoiceID = si.ID AND sioi.Deleted = 0
            JOIN dbo.SupplyOrder so ON so.ID = si.SupplyOrderID
            WHERE si.Deleted = 0
                  AND so.ClientID IS NOT NULL
                  AND sioi.ProductID IS NOT NULL
                  AND sioi.ProductID <> :syn

            UNION ALL

            SELECT COALESCE(soui.SupplierID, sou.SupplierID) AS producer_id,
                   soui.ProductID AS product_id,
                   sou.FromDate AS source_date
            FROM dbo.SupplyOrderUkraine sou
            JOIN dbo.SupplyOrderUkraineItem soui
              ON soui.SupplyOrderUkraineID = sou.ID AND soui.Deleted = 0
            WHERE sou.Deleted = 0
                  AND NOT (sou.IsFromCockpit = 1 AND sou.IsPlaced = 0)
                  AND COALESCE(soui.SupplierID, sou.SupplierID) IS NOT NULL
                  AND soui.ProductID IS NOT NULL
                  AND soui.ProductID <> :syn
        )
        SELECT DISTINCT producer_id AS pid
        FROM FactualSupply
        WHERE source_date >= DATEADD(day, -:days, :asof)
              AND source_date < :asof
        """,
        {"asof": as_of, "days": history_days, "syn": synthetic_product_id()},
    )
    return sorted(int(r["pid"]) for r in rows)


# Same filter as gba-server GetAllManufacturerClients (ManufactureTypeRoleId = 4),
# which feeds the console's /plan/producer picker.
_MANUFACTURER_ROLE_ID = 4


def manufacturer_producers() -> list[int]:
    """Active manufacturer clients — every producer the console can open /plan/producer for."""
    rows = query(
        """
        SELECT DISTINCT c.ID AS pid
        FROM dbo.Client c
        JOIN dbo.ClientInRole cir ON cir.ClientID = c.ID AND cir.Deleted = 0
        WHERE c.Deleted = 0 AND c.IsSubClient = 0 AND c.IsActive = 1
              AND cir.ClientTypeRoleID = :role
        """,
        {"role": _MANUFACTURER_ROLE_ID},
    )
    return [int(r["pid"]) for r in rows]


# --- source/business readiness ---

def _source_readiness_reason(metrics: dict) -> str | None:
    """Return the first actionable reason why today's procurement inputs are incomplete.

    A zero-line purchase plan is *not* an error by itself: it can mean every product has
    enough cover.  Readiness therefore gates on the factual inputs needed to make that
    conclusion, not on the output item count.
    """
    if (
        float(metrics.get("global_available_qty") or 0) > 0
        and int(metrics.get("role_marked_storage_count") or 0) <= 0
    ):
        return "storage_roles_missing"
    if int(metrics.get("sellable_storage_count") or 0) <= 0:
        return "sellable_storage_scope_unconfigured"
    if int(metrics.get("producer_count") or 0) <= 0:
        return "no_producer_candidates"
    if int(metrics.get("product_count") or 0) <= 0:
        return "no_product_candidates"
    if (
        int(metrics.get("unscoped_inventory_product_count") or 0) > 0
        and int(metrics.get("inventory_product_count") or 0) <= 0
    ):
        return "storage_scope_selects_no_inventory"
    if (
        int(metrics.get("inventory_product_count") or 0) <= 0
        or float(metrics.get("available_qty") or 0) <= 0
    ):
        return "sellable_inventory_missing"
    if int(metrics.get("demand_product_count") or 0) <= 0:
        return "no_recent_demand"
    if int(metrics.get("cost_product_count") or 0) <= 0:
        return "supplier_cost_history_missing"
    return None


def procurement_source_readiness(as_of: str, history_days: int) -> dict:
    """Compact, data-aware readiness snapshot for the current procurement plan.

    The query intentionally follows the same factual supply, demand, storage and price
    semantics as the plan repositories.  It catches a successful-but-empty 1C rebuild
    (or lost operational storage flags) before an empty plan can be cached as healthy.
    """
    rows = query(
        f"""
        WITH FactualSupply AS (
            SELECT CAST(0 AS tinyint) AS source_kind,
                   sioi.ID AS source_line_id,
                   so.ClientID AS producer_id,
                   sioi.ProductID AS product_id,
                   si.DateFrom AS source_date,
                   sioi.Qty AS source_qty,
                   sioi.UnitPrice AS unit_price,
                   ISNULL(a.CurrencyID, 2) AS source_currency_id,
                   sioi.Updated AS source_updated,
                   si.ID AS international_document_id,
                   CAST(NULL AS bigint) AS ukraine_document_id
            FROM dbo.SupplyInvoice si
            JOIN dbo.SupplyInvoiceOrderItem sioi
              ON sioi.SupplyInvoiceID = si.ID AND sioi.Deleted = 0
            JOIN dbo.SupplyOrder so ON so.ID = si.SupplyOrderID
            LEFT JOIN dbo.ClientAgreement ca ON ca.ID = so.ClientAgreementID
            LEFT JOIN dbo.Agreement a ON a.ID = ca.AgreementID
            WHERE si.Deleted = 0
                  AND so.ClientID IS NOT NULL
                  AND sioi.ProductID IS NOT NULL
                  AND sioi.ProductID <> :syn
                  AND si.DateFrom >= DATEADD(day, -:days, :asof)
                  AND si.DateFrom < :asof

            UNION ALL

            SELECT CAST(1 AS tinyint) AS source_kind,
                   soui.ID AS source_line_id,
                   COALESCE(soui.SupplierID, sou.SupplierID) AS producer_id,
                   soui.ProductID AS product_id,
                   sou.FromDate AS source_date,
                   soui.Qty AS source_qty,
                   soui.UnitPrice AS unit_price,
                   ISNULL(a.CurrencyID, 2) AS source_currency_id,
                   soui.Updated AS source_updated,
                   CAST(NULL AS bigint) AS international_document_id,
                   sou.ID AS ukraine_document_id
            FROM dbo.SupplyOrderUkraine sou
            JOIN dbo.SupplyOrderUkraineItem soui
              ON soui.SupplyOrderUkraineID = sou.ID AND soui.Deleted = 0
            LEFT JOIN dbo.ClientAgreement ca ON ca.ID = sou.ClientAgreementID
            LEFT JOIN dbo.Agreement a ON a.ID = ca.AgreementID
            WHERE sou.Deleted = 0
                  AND NOT (sou.IsFromCockpit = 1 AND sou.IsPlaced = 0)
                  AND COALESCE(soui.SupplierID, sou.SupplierID) IS NOT NULL
                  AND soui.ProductID IS NOT NULL
                  AND soui.ProductID <> :syn
                  AND sou.FromDate >= DATEADD(day, -:days, :asof)
                  AND sou.FromDate < :asof
        ),
        CandidatePairs AS (
            SELECT DISTINCT producer_id, product_id
            FROM FactualSupply
        ),
        CandidateProducts AS (
            SELECT DISTINCT product_id
            FROM CandidatePairs
        ),
        CandidateDemand AS (
            SELECT oi.ProductID AS product_id,
                   MAX(o.Created) AS latest_sale_date,
                   MAX(oi.ID) AS max_order_item_id,
                   MAX(oi.Updated) AS latest_order_item_update,
                   SUM(CAST(oi.Qty AS decimal(38, 6))) AS demand_qty,
                   CHECKSUM_AGG(BINARY_CHECKSUM(
                       oi.ID, oi.ProductID, oi.Qty, oi.PricePerItem,
                       oi.IsValidForCurrentSale, oi.Updated
                   )) AS demand_checksum
            FROM dbo.[Order] o
            JOIN dbo.OrderItem oi ON oi.OrderID = o.ID
            JOIN CandidateProducts cp ON cp.product_id = oi.ProductID
            WHERE oi.IsValidForCurrentSale = 1
                  AND o.Created >= DATEADD(day, -:days, :asof)
                  AND o.Created < :asof
            GROUP BY oi.ProductID
        ),
        SellableStorage AS (
            SELECT st.ID
            FROM dbo.Storage st
            WHERE {_SELLABLE_STORAGE}
        ),
        GlobalInventory AS (
            SELECT COUNT_BIG(*) AS availability_rows,
                   COUNT_BIG(DISTINCT pa.ProductID) AS product_count,
                   SUM(pa.Amount) AS available_qty,
                   MAX(pa.ID) AS max_availability_id,
                   MAX(pa.Updated) AS latest_availability_update
            FROM dbo.ProductAvailability pa
            JOIN dbo.Storage st ON st.ID = pa.StorageID
            WHERE pa.Deleted = 0
                  AND st.Deleted = 0
                  AND st.ForDefective = 0
        ),
        UnscopedCandidateInventory AS (
            SELECT COUNT_BIG(DISTINCT pa.ProductID) AS product_count
            FROM dbo.ProductAvailability pa
            JOIN dbo.Storage st ON st.ID = pa.StorageID
            JOIN CandidateProducts cp ON cp.product_id = pa.ProductID
            WHERE pa.Deleted = 0
                  AND st.Deleted = 0
                  AND st.ForDefective = 0
        ),
        CandidateInventory AS (
            SELECT pa.ProductID AS product_id,
                   COUNT_BIG(*) AS availability_rows,
                   SUM(pa.Amount) AS available_qty,
                   MAX(pa.ID) AS max_availability_id,
                   MAX(pa.Updated) AS latest_availability_update,
                   CHECKSUM_AGG(BINARY_CHECKSUM(
                       pa.ID, pa.ProductID, pa.StorageID, pa.Amount, pa.Updated
                   )) AS availability_checksum
            FROM dbo.ProductAvailability pa
            JOIN SellableStorage ss ON ss.ID = pa.StorageID
            JOIN CandidateProducts cp ON cp.product_id = pa.ProductID
            WHERE pa.Deleted = 0
            GROUP BY pa.ProductID
        ),
        CandidateReservations AS (
            SELECT COUNT_BIG(*) AS reservation_rows,
                   SUM(CAST(pr.Qty AS decimal(38, 6))) AS reserved_qty,
                   MAX(pr.ID) AS max_reservation_id,
                   MAX(pr.Updated) AS latest_reservation_update,
                   CHECKSUM_AGG(BINARY_CHECKSUM(
                       pr.ID, pa.ProductID, pa.StorageID, pr.Qty, pr.Updated
                   )) AS reservation_checksum
            FROM dbo.ProductReservation pr
            JOIN dbo.ProductAvailability pa
              ON pa.ID = pr.ProductAvailabilityID AND pa.Deleted = 0
            JOIN SellableStorage ss ON ss.ID = pa.StorageID
            JOIN CandidateProducts cp ON cp.product_id = pa.ProductID
            WHERE pr.Deleted = 0
        ),
        CandidateFlowFacts AS (
            SELECT CAST(0 AS tinyint) AS fact_kind,
                   plpoi.ID AS fact_id,
                   sioi.ProductID AS product_id,
                   plpoi.Qty AS qty,
                   plpoi.Updated AS fact_updated
            FROM dbo.PackingListPackageOrderItem plpoi
            JOIN dbo.SupplyInvoiceOrderItem sioi
              ON sioi.ID = plpoi.SupplyInvoiceOrderItemID AND sioi.Deleted = 0
            JOIN dbo.PackingList pl ON pl.ID = plpoi.PackingListID AND pl.Deleted = 0
            JOIN dbo.SupplyInvoice si ON si.ID = pl.SupplyInvoiceID AND si.Deleted = 0
            JOIN CandidateProducts cp ON cp.product_id = sioi.ProductID
            WHERE plpoi.Deleted = 0 AND si.DateFrom < :asof

            UNION ALL

            SELECT CAST(1 AS tinyint), pii.ID, sioi.ProductID, pii.Qty, pii.Updated
            FROM dbo.ProductIncomeItem pii
            JOIN dbo.ProductIncome pinc
              ON pinc.ID = pii.ProductIncomeID AND pinc.Deleted = 0
            JOIN dbo.PackingListPackageOrderItem plpoi
              ON plpoi.ID = pii.PackingListPackageOrderItemID AND plpoi.Deleted = 0
            JOIN dbo.SupplyInvoiceOrderItem sioi
              ON sioi.ID = plpoi.SupplyInvoiceOrderItemID AND sioi.Deleted = 0
            JOIN CandidateProducts cp ON cp.product_id = sioi.ProductID
            WHERE pii.Deleted = 0 AND pinc.FromDate < :asof

            UNION ALL

            SELECT CAST(2 AS tinyint), soui.ID, soui.ProductID, soui.Qty, soui.Updated
            FROM dbo.SupplyOrderUkraineItem soui
            JOIN dbo.SupplyOrderUkraine sou
              ON sou.ID = soui.SupplyOrderUkraineID AND sou.Deleted = 0
            JOIN CandidateProducts cp ON cp.product_id = soui.ProductID
            WHERE soui.Deleted = 0
                  AND NOT (sou.IsFromCockpit = 1 AND sou.IsPlaced = 0)
                  AND sou.FromDate < :asof

            UNION ALL

            SELECT CAST(3 AS tinyint), pii.ID, soui.ProductID, pii.Qty, pii.Updated
            FROM dbo.ProductIncomeItem pii
            JOIN dbo.ProductIncome pinc
              ON pinc.ID = pii.ProductIncomeID AND pinc.Deleted = 0
            JOIN dbo.SupplyOrderUkraineItem soui
              ON soui.ID = pii.SupplyOrderUkraineItemID AND soui.Deleted = 0
            JOIN dbo.SupplyOrderUkraine sou
              ON sou.ID = soui.SupplyOrderUkraineID AND sou.Deleted = 0
            JOIN CandidateProducts cp ON cp.product_id = soui.ProductID
            WHERE pii.Deleted = 0
                  AND NOT (sou.IsFromCockpit = 1 AND sou.IsPlaced = 0)
                  AND pinc.FromDate < :asof
        ),
        CandidateCosts AS (
            SELECT DISTINCT product_id
            FROM FactualSupply
            WHERE unit_price > 0
        )
        SELECT
            (SELECT COUNT_BIG(DISTINCT producer_id) FROM CandidatePairs) AS producer_count,
            (SELECT COUNT_BIG(*) FROM CandidatePairs) AS producer_product_pair_count,
            (SELECT COUNT_BIG(*) FROM CandidateProducts) AS product_count,
            (SELECT COUNT_BIG(*) FROM CandidateDemand) AS demand_product_count,
            (SELECT COUNT_BIG(*) FROM dbo.Storage st
             WHERE st.Deleted = 0 AND st.ForDefective = 0) AS active_storage_count,
            (SELECT COUNT_BIG(*) FROM dbo.Storage st
             WHERE st.Deleted = 0 AND st.ForDefective = 0
               AND (st.AvailableForReSale = 1 OR st.IsResale = 1))
                AS role_marked_storage_count,
            (SELECT COUNT_BIG(*) FROM SellableStorage) AS sellable_storage_count,
            (SELECT product_count FROM GlobalInventory) AS global_inventory_product_count,
            ISNULL((SELECT available_qty FROM GlobalInventory), 0) AS global_available_qty,
            (SELECT product_count FROM UnscopedCandidateInventory)
                AS unscoped_inventory_product_count,
            (SELECT COUNT_BIG(*) FROM CandidateInventory) AS inventory_product_count,
            ISNULL((SELECT SUM(availability_rows) FROM CandidateInventory), 0)
                AS availability_row_count,
            ISNULL((SELECT SUM(available_qty) FROM CandidateInventory), 0)
                AS available_qty,
            (SELECT COUNT_BIG(*) FROM CandidateCosts) AS cost_product_count,
            (SELECT COUNT_BIG(*) FROM FactualSupply) AS supply_line_count,
            ISNULL((SELECT SUM(CAST(source_qty AS decimal(38, 6))) FROM FactualSupply), 0)
                AS supply_qty,
            (SELECT CHECKSUM_AGG(BINARY_CHECKSUM(
                source_kind, source_line_id, producer_id, product_id, source_date,
                source_qty, unit_price, source_currency_id, source_updated
             )) FROM FactualSupply) AS supply_checksum,
            (SELECT MAX(source_updated) FROM FactualSupply) AS latest_supply_line_update,
            ISNULL((SELECT SUM(demand_qty) FROM CandidateDemand), 0) AS demand_qty,
            (SELECT CHECKSUM_AGG(BINARY_CHECKSUM(
                product_id, demand_qty, demand_checksum, latest_order_item_update
             )) FROM CandidateDemand) AS demand_checksum,
            (SELECT MAX(latest_order_item_update) FROM CandidateDemand)
                AS latest_order_item_update,
            (SELECT CHECKSUM_AGG(BINARY_CHECKSUM(
                product_id, availability_rows, available_qty,
                availability_checksum, latest_availability_update
             )) FROM CandidateInventory) AS availability_checksum,
            ISNULL((SELECT reservation_rows FROM CandidateReservations), 0)
                AS reservation_row_count,
            ISNULL((SELECT reserved_qty FROM CandidateReservations), 0) AS reserved_qty,
            ISNULL((SELECT max_reservation_id FROM CandidateReservations), 0)
                AS max_reservation_id,
            (SELECT latest_reservation_update FROM CandidateReservations)
                AS latest_reservation_update,
            ISNULL((SELECT reservation_checksum FROM CandidateReservations), 0)
                AS reservation_checksum,
            (SELECT COUNT_BIG(*) FROM CandidateFlowFacts) AS flow_fact_count,
            ISNULL((SELECT SUM(CAST(qty AS decimal(38, 6))) FROM CandidateFlowFacts), 0)
                AS flow_qty,
            (SELECT MAX(fact_id) FROM CandidateFlowFacts) AS max_flow_fact_id,
            (SELECT MAX(fact_updated) FROM CandidateFlowFacts) AS latest_flow_update,
            (SELECT CHECKSUM_AGG(BINARY_CHECKSUM(
                fact_kind, fact_id, product_id, qty, fact_updated
             )) FROM CandidateFlowFacts) AS flow_checksum,
            (SELECT CHECKSUM_AGG(BINARY_CHECKSUM(
                ID, CurrencyID, Code, Amount, Updated, Deleted
             )) FROM dbo.ExchangeRate) AS exchange_rate_checksum,
            (SELECT CHECKSUM_AGG(BINARY_CHECKSUM(
                ID, ExchangeRateID, Amount, Updated, Deleted
             )) FROM dbo.ExchangeRateHistory) AS exchange_rate_history_checksum,
            (SELECT CHECKSUM_AGG(BINARY_CHECKSUM(
                ID, CurrencyFromID, CurrencyToID, Amount, Updated, Deleted
             )) FROM dbo.CrossExchangeRate) AS cross_exchange_rate_checksum,
            (SELECT MAX(source_date) FROM FactualSupply) AS latest_supply_date,
            (SELECT MAX(latest_sale_date) FROM CandidateDemand) AS latest_sale_date,
            (SELECT MAX(international_document_id) FROM FactualSupply)
                AS max_international_document_id,
            (SELECT MAX(ukraine_document_id) FROM FactualSupply) AS max_ukraine_document_id,
            (SELECT MAX(max_order_item_id) FROM CandidateDemand) AS max_order_item_id,
            (SELECT MAX(ID) FROM SellableStorage) AS max_sellable_storage_id,
            (SELECT MAX(max_availability_id) FROM CandidateInventory)
                AS max_candidate_availability_id,
            (SELECT MAX(latest_availability_update) FROM CandidateInventory)
                AS latest_candidate_availability_update,
            (SELECT max_availability_id FROM GlobalInventory) AS max_global_availability_id,
            (SELECT latest_availability_update FROM GlobalInventory)
                AS latest_global_availability_update
        """,
        {"asof": as_of, "days": history_days, "syn": synthetic_product_id()},
    )
    row = rows[0] if rows else {}
    integer_fields = (
        "producer_count",
        "producer_product_pair_count",
        "product_count",
        "demand_product_count",
        "active_storage_count",
        "role_marked_storage_count",
        "sellable_storage_count",
        "global_inventory_product_count",
        "unscoped_inventory_product_count",
        "inventory_product_count",
        "availability_row_count",
        "cost_product_count",
        "supply_line_count",
        "supply_checksum",
        "demand_checksum",
        "availability_checksum",
        "reservation_row_count",
        "max_reservation_id",
        "reservation_checksum",
        "flow_fact_count",
        "max_flow_fact_id",
        "flow_checksum",
        "exchange_rate_checksum",
        "exchange_rate_history_checksum",
        "cross_exchange_rate_checksum",
        "max_international_document_id",
        "max_ukraine_document_id",
        "max_order_item_id",
        "max_sellable_storage_id",
        "max_candidate_availability_id",
        "max_global_availability_id",
    )
    snapshot = {name: int(row.get(name) or 0) for name in integer_fields}
    snapshot["available_qty"] = round(float(row.get("available_qty") or 0), 3)
    snapshot["global_available_qty"] = round(
        float(row.get("global_available_qty") or 0), 3
    )
    for name in ("supply_qty", "demand_qty", "reserved_qty", "flow_qty"):
        snapshot[name] = round(float(row.get(name) or 0), 6)
    for name in (
        "latest_supply_date",
        "latest_sale_date",
        "latest_supply_line_update",
        "latest_order_item_update",
        "latest_reservation_update",
        "latest_flow_update",
        "latest_candidate_availability_update",
        "latest_global_availability_update",
    ):
        value = row.get(name)
        snapshot[name] = value.isoformat() if hasattr(value, "isoformat") else (
            str(value) if value is not None else None
        )
    fingerprint_fields = {
        name: snapshot[name]
        for name in (
            "producer_product_pair_count",
            "product_count",
            "demand_product_count",
            "sellable_storage_count",
            "inventory_product_count",
            "availability_row_count",
            "available_qty",
            "supply_line_count",
            "supply_qty",
            "supply_checksum",
            "latest_supply_line_update",
            "demand_qty",
            "demand_checksum",
            "latest_order_item_update",
            "availability_checksum",
            "reservation_row_count",
            "reserved_qty",
            "max_reservation_id",
            "reservation_checksum",
            "latest_reservation_update",
            "flow_fact_count",
            "flow_qty",
            "max_flow_fact_id",
            "flow_checksum",
            "latest_flow_update",
            "exchange_rate_checksum",
            "exchange_rate_history_checksum",
            "cross_exchange_rate_checksum",
            "latest_supply_date",
            "latest_sale_date",
            "max_international_document_id",
            "max_ukraine_document_id",
            "max_order_item_id",
            "max_sellable_storage_id",
            "max_candidate_availability_id",
            "latest_candidate_availability_update",
        )
    }
    snapshot["source_fingerprint"] = hashlib.sha256(
        json.dumps(fingerprint_fields, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:24]
    reason = _source_readiness_reason(snapshot)
    snapshot.update(
        {
            "ready": reason is None,
            "reason": reason,
            "as_of": as_of,
            "history_days": history_days,
        }
    )
    return snapshot


# --- ABC revenue ranking (global, EUR) ---

def all_products_revenue_eur(as_of: str, history_days: int) -> dict[int, float]:
    """Trailing realized revenue (EUR) per product. PricePerItem is already EUR."""
    rows = query(
        """
        SELECT oi.ProductID AS pid, SUM(oi.Qty * oi.PricePerItem) AS rev
        FROM dbo.[Order] o
        JOIN dbo.OrderItem oi ON oi.OrderID = o.ID
        WHERE oi.IsValidForCurrentSale = 1
              AND oi.ProductID <> :syn
              AND oi.PricePerItem > 0
              AND o.Created < :asof
              AND o.Created >= DATEADD(day, -:days, :asof)
        GROUP BY oi.ProductID
        """,
        {"asof": as_of, "days": history_days, "syn": synthetic_product_id()},
    )
    return {int(r["pid"]): float(r["rev"] or 0) for r in rows}


# --- inventory position ---

_SELLABLE_STORAGE = (
    "st.Deleted = 0 AND st.ForDefective = 0 "
    "AND (st.AvailableForReSale = 1 OR st.IsResale = 1)"
)


def on_hand(product_ids: list[int]) -> dict[int, float]:
    """Gross physical on-hand in operational sellable storages.

    ``ProductAvailability.Amount`` is already *net/free* stock: gba-server subtracts a
    reservation when it creates ``ProductReservation`` and adds it back on release.
    Reconstruct gross on-hand by adding active reservations once; policy then computes
    ``available = on_hand - reserved`` without the historical double-subtraction bug.
    """
    if not product_ids:
        return {}
    ph, params = in_clause("p", product_ids)
    rows = query(
        f"""
        WITH ReservedByAvailability AS (
            SELECT pr.ProductAvailabilityID, SUM(pr.Qty) AS qty
            FROM dbo.ProductReservation pr
            WHERE pr.Deleted = 0
            GROUP BY pr.ProductAvailabilityID
        )
        SELECT pa.ProductID AS pid,
               SUM(pa.Amount + ISNULL(r.qty, 0)) AS amt
        FROM dbo.ProductAvailability pa
        JOIN dbo.Storage st ON st.ID = pa.StorageID
        LEFT JOIN ReservedByAvailability r ON r.ProductAvailabilityID = pa.ID
        WHERE pa.Deleted = 0 AND pa.ProductID IN {ph}
              AND {_SELLABLE_STORAGE}
        GROUP BY pa.ProductID
        """,
        params,
    )
    return {int(r["pid"]): float(r["amt"] or 0) for r in rows}


def reserved(product_ids: list[int]) -> dict[int, float]:
    """Reserved qty per product (ProductReservation -> ProductAvailability -> ProductID)."""
    if not product_ids:
        return {}
    ph, params = in_clause("p", product_ids)
    rows = query(
        f"""
        SELECT pa.ProductID AS pid, SUM(pr.Qty) AS qty
        FROM dbo.ProductReservation pr
        JOIN dbo.ProductAvailability pa ON pa.ID = pr.ProductAvailabilityID
        JOIN dbo.Storage st ON st.ID = pa.StorageID
        WHERE pr.Deleted = 0 AND pa.Deleted = 0 AND pa.ProductID IN {ph}
              AND {_SELLABLE_STORAGE}
        GROUP BY pa.ProductID
        """,
        params,
    )
    return {int(r["pid"]): float(r["qty"] or 0) for r in rows}


# on_order references the IN list 4x in one statement (2 ordered + 2 received sub-selects);
# chunk well under MSSQL's 2100-param cap and to keep the query-processor plan tractable:
# 4 * 400 + 1 (asof) << 2100.
_ON_ORDER_IN_CHUNK = 400


def on_order(product_ids: list[int], as_of: str) -> dict[int, float]:
    """In-transit (packed/committed but not yet received) qty at ``as_of``.

    on_order(p) = ordered(p, < as_of) - received(p, < as_of), clamped >= 0, per REAL product,
    summed over BOTH supply chains:

      INTERNATIONAL spine (real product detail lives on the packing list, NOT on
      SupplyOrderItem — that table only carries a synthetic placeholder row for an order that
      has not yet arrived, so the old query was structurally empty):
        ordered  = PackingListPackageOrderItem.Qty
                   -> SupplyInvoiceOrderItem(ProductID)  (real product + ordered qty)
                   -> PackingList -> SupplyInvoice(DateFrom = real placement date)
        received = ProductIncomeItem.Qty                  (receipt into stock)
                   -> ProductIncome(FromDate = real receipt date)
                   linked to the same PackingListPackageOrderItem
      UKRAINE spine (domestic; SupplyOrderUkraineItem carries real product + ordered qty):
        ordered  = SupplyOrderUkraineItem.Qty -> SupplyOrderUkraine(FromDate)
        received = ProductIncomeItem.Qty -> SupplyOrderUkraineItemID, ProductIncome(FromDate)

    Why the rewrite was needed: the old query filtered dbo.SupplyOrder.Created < :asof, but
    Created is the 1C-sync timestamp (rewritten to ~now on every sync) -- so the filter excluded
    every row and on_order was always {}. It also read the synthetic SupplyOrderItem detail.
    Both DateFrom/FromDate columns ARE real historical dates, so netting ordered<as_of against
    received<as_of yields the genuine in-transit quantity outstanding at the point in time.

    Trap honored: PricePerItem/EUR not involved here (units only); the synthetic debt product
    excluded; supply-side Deleted=0 on every joined table (verified, not blanket-applied).
    """
    if not product_ids:
        return {}
    syn = synthetic_product_id()
    ids = [int(p) for p in product_ids if int(p) != syn]
    out: dict[int, float] = {}
    for start in range(0, len(ids), _ON_ORDER_IN_CHUNK):
        chunk = ids[start : start + _ON_ORDER_IN_CHUNK]
        out.update(_on_order_chunk(chunk, as_of))
    return out


def _on_order_chunk(product_ids: list[int], as_of: str) -> dict[int, float]:
    ph, params = in_clause("p", product_ids)
    rows = query(
        f"""
        WITH ordered AS (
            -- international spine: ordered qty on the packing list, real product
            SELECT sioi.ProductID AS pid, SUM(plpoi.Qty) AS qty
            FROM dbo.PackingListPackageOrderItem plpoi
            JOIN dbo.SupplyInvoiceOrderItem sioi
                 ON sioi.ID = plpoi.SupplyInvoiceOrderItemID AND sioi.Deleted = 0
            JOIN dbo.PackingList pl ON pl.ID = plpoi.PackingListID AND pl.Deleted = 0
            JOIN dbo.SupplyInvoice si ON si.ID = pl.SupplyInvoiceID AND si.Deleted = 0
            WHERE plpoi.Deleted = 0
                  AND sioi.ProductID <> :syn
                  AND sioi.ProductID IN {ph}
                  AND si.DateFrom < :asof
            GROUP BY sioi.ProductID
            UNION ALL
            -- ukraine spine: ordered qty on the domestic supply order item
            SELECT soui.ProductID AS pid, SUM(soui.Qty) AS qty
            FROM dbo.SupplyOrderUkraineItem soui
            JOIN dbo.SupplyOrderUkraine sou
                 ON sou.ID = soui.SupplyOrderUkraineID AND sou.Deleted = 0
            WHERE soui.Deleted = 0
                  AND NOT (sou.IsFromCockpit = 1 AND sou.IsPlaced = 0)
                  AND soui.ProductID <> :syn
                  AND soui.ProductID IN {ph}
                  AND sou.FromDate < :asof
            GROUP BY soui.ProductID
        ),
        received AS (
            -- international spine: receipts netted via the packing-list line
            SELECT sioi.ProductID AS pid, SUM(pii.Qty) AS qty
            FROM dbo.ProductIncomeItem pii
            JOIN dbo.ProductIncome pinc
                 ON pinc.ID = pii.ProductIncomeID AND pinc.Deleted = 0
            JOIN dbo.PackingListPackageOrderItem plpoi
                 ON plpoi.ID = pii.PackingListPackageOrderItemID AND plpoi.Deleted = 0
            JOIN dbo.SupplyInvoiceOrderItem sioi
                 ON sioi.ID = plpoi.SupplyInvoiceOrderItemID AND sioi.Deleted = 0
            WHERE pii.Deleted = 0
                  AND sioi.ProductID <> :syn
                  AND sioi.ProductID IN {ph}
                  AND pinc.FromDate < :asof
            GROUP BY sioi.ProductID
            UNION ALL
            -- ukraine spine: receipts netted via the domestic supply order item
            SELECT soui.ProductID AS pid, SUM(pii.Qty) AS qty
            FROM dbo.ProductIncomeItem pii
            JOIN dbo.ProductIncome pinc
                 ON pinc.ID = pii.ProductIncomeID AND pinc.Deleted = 0
            JOIN dbo.SupplyOrderUkraineItem soui
                 ON soui.ID = pii.SupplyOrderUkraineItemID AND soui.Deleted = 0
            WHERE pii.Deleted = 0
                  AND soui.ProductID <> :syn
                  AND soui.ProductID IN {ph}
                  AND pinc.FromDate < :asof
            GROUP BY soui.ProductID
        ),
        ord_g AS (SELECT pid, SUM(qty) AS qty FROM ordered GROUP BY pid),
        rcv_g AS (SELECT pid, SUM(qty) AS qty FROM received GROUP BY pid)
        SELECT o.pid AS pid, (o.qty - ISNULL(r.qty, 0)) AS qty
        FROM ord_g o
        LEFT JOIN rcv_g r ON r.pid = o.pid
        WHERE (o.qty - ISNULL(r.qty, 0)) > 0.001
        """,
        {"asof": as_of, "syn": synthetic_product_id(), **params},
    )
    return {int(r["pid"]): float(r["qty"] or 0) for r in rows}
