"""EUR unit-cost layer over supplier price history.

SupplyOrderItem.UnitPrice is in the supplier agreement currency
(SupplyOrder -> ClientAgreement -> Agreement.CurrencyID); converted to EUR via
dbo.GetExchangedToEuroValue at the order placement date (ISNULL(DateFrom, as_of)).
"""
from __future__ import annotations

from statistics import median

from app.core.logging import get_logger
from app.data.db import in_clause, query

log = get_logger("cost_repository")

SYNTHETIC_PRODUCT_ID = 25422404
_IN_CHUNK = 800
_HISTORY_DAYS = 540


def _fetch_cost_rows(
    product_ids: list[int], as_of: str, history_days: int, producer_id: int | None = None
) -> list[dict]:
    ids = [int(p) for p in product_ids if int(p) != SYNTHETIC_PRODUCT_ID]
    out: list[dict] = []
    for start in range(0, len(ids), _IN_CHUNK):
        chunk = ids[start : start + _IN_CHUNK]
        ph, params = in_clause("p", chunk)
        params.update({"asof": as_of, "days": history_days})
        producer_filter = "AND so.ClientID IS NOT NULL"
        if producer_id is not None:
            producer_filter = "AND so.ClientID = :pid"
            params["pid"] = producer_id
        rows = query(
            f"""
            SELECT soi.ProductID AS pid, so.ClientID AS producer_id,
                   dbo.GetExchangedToEuroValue(
                       soi.UnitPrice, ISNULL(a.CurrencyID, 2), ISNULL(so.DateFrom, :asof)
                   ) AS cost_eur
            FROM dbo.SupplyOrder so
            JOIN dbo.SupplyOrderItem soi ON soi.SupplyOrderID = so.ID
            LEFT JOIN dbo.ClientAgreement ca ON ca.ID = so.ClientAgreementID
            LEFT JOIN dbo.Agreement a ON a.ID = ca.AgreementID
            WHERE so.Deleted = 0 AND soi.Deleted = 0
                  AND soi.UnitPrice > 0
                  AND soi.ProductID <> 25422404
                  AND soi.ProductID IN {ph}
                  {producer_filter}
                  AND ISNULL(so.DateFrom, so.Created) >= DATEADD(day, -:days, :asof)
                  AND ISNULL(so.DateFrom, so.Created) < :asof
            """,
            params,
        )
        out.extend(rows)
    return out


def producer_unit_costs_eur(
    producer_id: int, product_ids: list[int], as_of: str, history_days: int = _HISTORY_DAYS
) -> dict[int, float]:
    """Median EUR unit cost per product for one producer."""
    if not product_ids:
        return {}
    by_pid: dict[int, list[float]] = {}
    for r in _fetch_cost_rows(product_ids, as_of, history_days, producer_id=producer_id):
        c = r["cost_eur"]
        if c is not None and float(c) > 0:
            by_pid.setdefault(int(r["pid"]), []).append(float(c))
    return {pid: round(median(v), 4) for pid, v in by_pid.items() if v}


def sale_prices_eur(
    product_ids: list[int], as_of: str, history_days: int
) -> dict[int, float]:
    """Median realized sale price (EUR) per product. OrderItem.PricePerItem is already EUR."""
    if not product_ids:
        return {}
    ids = [int(p) for p in product_ids if int(p) != SYNTHETIC_PRODUCT_ID]
    by_pid: dict[int, list[float]] = {}
    for start in range(0, len(ids), _IN_CHUNK):
        chunk = ids[start : start + _IN_CHUNK]
        ph, params = in_clause("p", chunk)
        params.update({"asof": as_of, "days": history_days})
        rows = query(
            f"""
            SELECT oi.ProductID AS pid, oi.PricePerItem AS price
            FROM dbo.[Order] o
            JOIN dbo.OrderItem oi ON oi.OrderID = o.ID
            WHERE oi.IsValidForCurrentSale = 1
                  AND oi.PricePerItem > 0
                  AND oi.ProductID <> 25422404
                  AND oi.ProductID IN {ph}
                  AND o.Created < :asof
                  AND o.Created >= DATEADD(day, -:days, :asof)
            """,
            params,
        )
        for r in rows:
            by_pid.setdefault(int(r["pid"]), []).append(float(r["price"]))
    return {pid: round(median(v), 4) for pid, v in by_pid.items() if v}


def cheapest_alt_eur(
    product_ids: list[int], as_of: str, history_days: int = _HISTORY_DAYS
) -> dict[int, dict]:
    """Per product, the producer with the lowest median EUR cost (cross-supplier)."""
    if not product_ids:
        return {}
    pair: dict[tuple[int, int], list[float]] = {}
    for r in _fetch_cost_rows(product_ids, as_of, history_days, producer_id=None):
        c = r["cost_eur"]
        if c is None or float(c) <= 0:
            continue
        pair.setdefault((int(r["pid"]), int(r["producer_id"])), []).append(float(c))
    best: dict[int, tuple[int, float]] = {}
    for (pid, producer), costs in pair.items():
        m = median(costs)
        cur = best.get(pid)
        if cur is None or m < cur[1]:
            best[pid] = (producer, round(m, 4))
    return {pid: {"producer_id": pr, "cost_eur": c} for pid, (pr, c) in best.items()}
