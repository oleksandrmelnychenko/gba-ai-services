"""Parameterized read queries over the procurement spine.

Verified columns (ConcordDb_V5):
  SupplyOrder(OrganizationID, DateFrom, Created, OrderArrivedDate, IsOrderArrived, ...)
    FK_SupplyOrder_Organization_OrganizationID -> dbo.Organization(ID, Name)
    DateFrom = real placement date; Created = 1C sync timestamp (rewritten to ~now)
  SupplyOrderItem(SupplyOrderID, ProductID, Qty)
  Organization(ID, Name)  -- supplier names (NOT dbo.SupplyOrganization, 0 overlap)
  ProductAvailability(ProductID, StorageID, Amount)
  ProductReservation(ProductAvailabilityID, Qty)  -- links to product via ProductAvailability
  Order/OrderItem -- demand history (sales); filter oi.IsValidForCurrentSale=1
  ProductID 25422404 = synthetic, excluded from sales/demand/supply candidates

  ON-ORDER spine (real per-product detail; SupplyOrderItem holds only a synthetic placeholder
  for not-yet-arrived orders, so it CANNOT source on_order):
    SupplyOrder -> SupplyInvoice(DateFrom) -> PackingList -> PackingListPackageOrderItem(Qty)
       -> SupplyInvoiceOrderItem(ProductID)            == ordered, real product
    ProductIncome(FromDate) -> ProductIncomeItem(Qty)  == received (netted)
       linked via PackingListPackageOrderItemID (intl) or SupplyOrderUkraineItemID (UA)
    SupplyOrderUkraine(FromDate) -> SupplyOrderUkraineItem(ProductID, Qty)  == UA ordered
  DateFrom/FromDate are REAL historical dates; SupplyOrder.Created is the 1C-sync stamp (~now).
"""
from __future__ import annotations

from app.core.logging import get_logger
from app.data.db import in_clause, query

log = get_logger("supply_repository")

SYNTHETIC_PRODUCT_ID = 25422404

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
              AND oi.ProductID <> 25422404
              AND o.Created < :asof
              AND o.Created >= DATEADD(day, -:days, :asof)
        GROUP BY CAST(o.Created AS date)
        ORDER BY d
        """,
        {"pid": product_id, "asof": as_of, "days": history_days},
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
    Same filters as product_daily_demand: IsValidForCurrentSale=1, exclude 25422404, no o.Deleted.
    """
    out: dict[int, list[dict]] = {}
    if not product_ids:
        return out
    ids = [int(p) for p in product_ids if int(p) != SYNTHETIC_PRODUCT_ID]
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
                  AND oi.ProductID <> 25422404
                  AND o.Created < :asof
                  AND o.Created >= DATEADD(day, -:days, :asof)
            GROUP BY oi.ProductID, CAST(o.Created AS date)
            ORDER BY oi.ProductID, CAST(o.Created AS date)
            """,
            {"asof": as_of, "days": history_days, **params},
        )
        for r in rows:
            out.setdefault(int(r["pid"]), []).append({"d": r["d"], "units": r["units"]})
    return out


# --- lead time per producer (DateFrom -> OrderArrivedDate) ---

def producer_lead_times(
    producer_id: int, as_of: str, min_days: int = 1, max_days: int = 120
) -> list[float]:
    """Plausible lead times per arrived supply order, keyed on the producer (ClientID)."""
    rows = query(
        """
        SELECT DATEDIFF(day, so.DateFrom, so.OrderArrivedDate) AS lead_days
        FROM dbo.SupplyOrder so
        WHERE so.ClientID = :pid AND so.Deleted = 0
              AND so.IsOrderArrived = 1
              AND so.DateFrom IS NOT NULL
              AND so.OrderArrivedDate IS NOT NULL
              AND so.DateFrom < :asof
              AND DATEDIFF(day, so.DateFrom, so.OrderArrivedDate) BETWEEN :lmin AND :lmax
        """,
        {"pid": producer_id, "asof": as_of, "lmin": min_days, "lmax": max_days},
    )
    leads = [float(r["lead_days"]) for r in rows]
    log.info("producer_lead_times", producer_id=producer_id, sample_count=len(leads))
    return leads


def producer_agreement_currency(producer_id: int) -> int | None:
    """Modal agreement currency for the producer's supply orders (geography proxy)."""
    rows = query(
        """
        SELECT TOP 1 a.CurrencyID AS ccy
        FROM dbo.SupplyOrder so
        JOIN dbo.ClientAgreement ca ON ca.ID = so.ClientAgreementID
        JOIN dbo.Agreement a ON a.ID = ca.AgreementID
        WHERE so.ClientID = :pid AND so.Deleted = 0 AND a.CurrencyID IS NOT NULL
        GROUP BY a.CurrencyID
        ORDER BY COUNT(*) DESC
        """,
        {"pid": producer_id},
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


def products_for_producer(producer_id: int, as_of: str, history_days: int) -> list[int]:
    """Products this producer has supplied (candidates for replenishment)."""
    rows = query(
        """
        SELECT DISTINCT soi.ProductID AS pid
        FROM dbo.SupplyOrder so
        JOIN dbo.SupplyOrderItem soi ON soi.SupplyOrderID = so.ID
        WHERE so.ClientID = :pid AND so.Deleted = 0 AND soi.Deleted = 0
              AND soi.ProductID IS NOT NULL
              AND soi.ProductID <> 25422404
              AND so.DateFrom >= DATEADD(day, -:days, :asof)
        """,
        {"pid": producer_id, "asof": as_of, "days": history_days},
    )
    return [int(r["pid"]) for r in rows]


def derive_moq_terms(min_orders: int = 3) -> list[dict]:
    """Per (producer, product) with >= min_orders supply orders: observed MOQ = MIN(Qty),
    pack multiple from Product.PackingStandard. Source for seeding buyer masters."""
    rows = query(
        """
        SELECT so.ClientID AS producer_id, soi.ProductID AS product_id,
               MIN(soi.Qty) AS moq, COUNT(*) AS orders,
               TRY_CONVERT(decimal(18,3), MAX(p.PackingStandard)) AS pack
        FROM dbo.SupplyOrder so
        JOIN dbo.SupplyOrderItem soi ON soi.SupplyOrderID = so.ID
        LEFT JOIN dbo.Product p ON p.ID = soi.ProductID
        WHERE so.Deleted = 0 AND soi.Deleted = 0 AND so.ClientID IS NOT NULL
              AND soi.ProductID <> 25422404 AND soi.Qty > 0
        GROUP BY so.ClientID, soi.ProductID
        HAVING COUNT(*) >= :n
        """,
        {"n": min_orders},
    )
    return [
        {"producer_id": int(r["producer_id"]), "product_id": int(r["product_id"]),
         "moq": float(r["moq"]), "orders": int(r["orders"]),
         "pack": float(r["pack"]) if r["pack"] is not None else None}
        for r in rows
    ]


def all_producers(as_of: str, history_days: int) -> list[int]:
    """Producers that supplied within the history window (candidates for cart replenishment)."""
    rows = query(
        """
        SELECT DISTINCT so.ClientID AS pid
        FROM dbo.SupplyOrder so
        JOIN dbo.SupplyOrderItem soi ON soi.SupplyOrderID = so.ID
        WHERE so.Deleted = 0 AND soi.Deleted = 0
              AND so.ClientID IS NOT NULL
              AND soi.ProductID IS NOT NULL
              AND soi.ProductID <> 25422404
              AND so.DateFrom >= DATEADD(day, -:days, :asof)
        """,
        {"asof": as_of, "days": history_days},
    )
    return [int(r["pid"]) for r in rows]


# --- ABC revenue ranking (global, EUR) ---

def all_products_revenue_eur(as_of: str, history_days: int) -> dict[int, float]:
    """Trailing realized revenue (EUR) per product. PricePerItem is already EUR."""
    rows = query(
        """
        SELECT oi.ProductID AS pid, SUM(oi.Qty * oi.PricePerItem) AS rev
        FROM dbo.[Order] o
        JOIN dbo.OrderItem oi ON oi.OrderID = o.ID
        WHERE oi.IsValidForCurrentSale = 1
              AND oi.ProductID <> 25422404
              AND oi.PricePerItem > 0
              AND o.Created < :asof
              AND o.Created >= DATEADD(day, -:days, :asof)
        GROUP BY oi.ProductID
        """,
        {"asof": as_of, "days": history_days},
    )
    return {int(r["pid"]): float(r["rev"] or 0) for r in rows}


# --- inventory position ---

_SELLABLE_STORAGE = (
    "(st.ForEcommerce = 1 OR st.AvailableForReSale = 1 OR st.IsResale = 1)"
)


def on_hand(product_ids: list[int]) -> dict[int, float]:
    if not product_ids:
        return {}
    ph, params = in_clause("p", product_ids)
    rows = query(
        f"""
        SELECT pa.ProductID AS pid, SUM(pa.Amount) AS amt
        FROM dbo.ProductAvailability pa
        JOIN dbo.Storage st ON st.ID = pa.StorageID
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
    """Open (ordered-but-not-yet-received) qty per product, point-in-time at as_of.

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

    Trap honored: PricePerItem/EUR not involved here (units only); synthetic ProductID 25422404
    excluded; supply-side Deleted=0 on every joined table (verified, not blanket-applied).
    """
    if not product_ids:
        return {}
    ids = [int(p) for p in product_ids if int(p) != SYNTHETIC_PRODUCT_ID]
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
                  AND sioi.ProductID <> 25422404
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
                  AND soui.ProductID <> 25422404
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
                  AND sioi.ProductID <> 25422404
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
                  AND soui.ProductID <> 25422404
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
        {"asof": as_of, **params},
    )
    return {int(r["pid"]): float(r["qty"] or 0) for r in rows}
