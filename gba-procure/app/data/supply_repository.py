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
              AND so.Created >= DATEADD(day, -:days, :asof)
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
              AND so.Created >= DATEADD(day, -:days, :asof)
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


def on_order(product_ids: list[int], as_of: str) -> dict[int, float]:
    """Open supply-order qty per product (ordered, not yet arrived)."""
    if not product_ids:
        return {}
    ph, params = in_clause("p", product_ids)
    rows = query(
        f"""
        SELECT soi.ProductID AS pid, SUM(soi.Qty) AS qty
        FROM dbo.SupplyOrder so
        JOIN dbo.SupplyOrderItem soi ON soi.SupplyOrderID = so.ID
        WHERE so.Deleted = 0 AND soi.Deleted = 0
              AND so.IsOrderArrived = 0
              AND soi.ProductID <> 25422404
              AND so.Created < :asof
              AND soi.ProductID IN {ph}
        GROUP BY soi.ProductID
        """,
        {"asof": as_of, **params},
    )
    return {int(r["pid"]): float(r["qty"] or 0) for r in rows}
