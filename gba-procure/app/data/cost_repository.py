"""EUR unit-cost layer over authoritative supplier price history.

International product detail and price live on ``SupplyInvoiceOrderItem``; the
parent ``SupplyOrderItem`` is only a synthetic debt placeholder in the current
1C import. Domestic detail lives on ``SupplyOrderUkraineItem``. Both prices are
converted from the supplier agreement currency with the source document date.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from statistics import median

from app.core.history import history_start_iso
from app.core.logging import get_logger
from app.data.db import in_clause, query
from app.data.synthetic import synthetic_product_id

log = get_logger("cost_repository")

# The product IN-list is used by both UNION branches. Keep 2 * chunk plus the
# scalar parameters safely below SQL Server's 2100-parameter statement limit.
_IN_CHUNK = 800
_HISTORY_DAYS = 540
_FOUR_DECIMALS = Decimal("0.0001")


def _as_decimal(value: object) -> Decimal:
    """Preserve SQL decimal values exactly; never round money through binary float."""
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _rounded_float(value: Decimal, quantum: Decimal = _FOUR_DECIMALS) -> float:
    return float(value.quantize(quantum, rounding=ROUND_HALF_UP))


def _fetch_cost_rows(
    product_ids: list[int], as_of: str, history_days: int, producer_id: int | None = None
) -> list[dict]:
    syn = synthetic_product_id()
    ids = [int(p) for p in product_ids if int(p) != syn]
    out: list[dict] = []
    for start in range(0, len(ids), _IN_CHUNK):
        chunk = ids[start : start + _IN_CHUNK]
        ph, params = in_clause("p", chunk)
        params.update(
            {
                "asof": as_of,
                "days": history_days,
                "history_start": history_start_iso(),
                "syn": syn,
            }
        )
        international_producer_filter = "AND so.ClientID IS NOT NULL"
        ukraine_producer_filter = "AND COALESCE(soui.SupplierID, sou.SupplierID) IS NOT NULL"
        if producer_id is not None:
            international_producer_filter = "AND so.ClientID = :pid"
            ukraine_producer_filter = (
                "AND (soui.SupplierID = :pid "
                "OR (soui.SupplierID IS NULL AND sou.SupplierID = :pid))"
            )
            params["pid"] = producer_id
        rows = query(
            f"""
            SELECT sioi.ProductID AS pid, so.ClientID AS producer_id,
                   dbo.GetExchangedToEuroValue(
                       sioi.UnitPrice, ISNULL(a.CurrencyID, 2), si.DateFrom
                   ) AS cost_eur
            FROM dbo.SupplyOrder so
            JOIN dbo.SupplyInvoice si
                 ON si.SupplyOrderID = so.ID AND si.Deleted = 0
            JOIN dbo.SupplyInvoiceOrderItem sioi
                 ON sioi.SupplyInvoiceID = si.ID AND sioi.Deleted = 0
            LEFT JOIN dbo.ClientAgreement ca ON ca.ID = so.ClientAgreementID
            LEFT JOIN dbo.Agreement a ON a.ID = ca.AgreementID
            WHERE sioi.UnitPrice > 0
                  AND sioi.ProductID <> :syn
                  AND sioi.ProductID IN {ph}
                  {international_producer_filter}
                  AND si.DateFrom >= DATEADD(day, -:days, :asof)
                  AND si.DateFrom >= :history_start
                  AND si.DateFrom < :asof

            UNION ALL

            SELECT soui.ProductID AS pid,
                   COALESCE(soui.SupplierID, sou.SupplierID) AS producer_id,
                   dbo.GetExchangedToEuroValue(
                       soui.UnitPrice, ISNULL(a.CurrencyID, 2), sou.FromDate
                   ) AS cost_eur
            FROM dbo.SupplyOrderUkraine sou
            JOIN dbo.SupplyOrderUkraineItem soui
                 ON soui.SupplyOrderUkraineID = sou.ID AND soui.Deleted = 0
            LEFT JOIN dbo.ClientAgreement ca ON ca.ID = sou.ClientAgreementID
            LEFT JOIN dbo.Agreement a ON a.ID = ca.AgreementID
            WHERE sou.Deleted = 0
                  AND NOT (sou.IsFromCockpit = 1 AND sou.IsPlaced = 0)
                  AND soui.UnitPrice > 0
                  AND soui.ProductID <> :syn
                  AND soui.ProductID IN {ph}
                  {ukraine_producer_filter}
                  AND sou.FromDate >= DATEADD(day, -:days, :asof)
                  AND sou.FromDate >= :history_start
                  AND sou.FromDate < :asof
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
    by_pid: dict[int, list[Decimal]] = {}
    for r in _fetch_cost_rows(product_ids, as_of, history_days, producer_id=producer_id):
        c = r["cost_eur"]
        if c is not None and _as_decimal(c) > 0:
            by_pid.setdefault(int(r["pid"]), []).append(_as_decimal(c))
    return {pid: _rounded_float(median(v)) for pid, v in by_pid.items() if v}


def sale_prices_eur(
    product_ids: list[int], as_of: str, history_days: int
) -> dict[int, float]:
    """Median realized sale price (EUR) per product. OrderItem.PricePerItem is already EUR."""
    if not product_ids:
        return {}
    syn = synthetic_product_id()
    ids = [int(p) for p in product_ids if int(p) != syn]
    by_pid: dict[int, list[Decimal]] = {}
    for start in range(0, len(ids), _IN_CHUNK):
        chunk = ids[start : start + _IN_CHUNK]
        ph, params = in_clause("p", chunk)
        params.update(
            {
                "asof": as_of,
                "days": history_days,
                "history_start": history_start_iso(),
                "syn": syn,
            }
        )
        rows = query(
            f"""
            SELECT oi.ProductID AS pid, oi.PricePerItem AS price
            FROM dbo.[Order] o
            JOIN dbo.OrderItem oi ON oi.OrderID = o.ID
            WHERE oi.IsValidForCurrentSale = 1
                  AND oi.PricePerItem > 0
                  AND oi.ProductID <> :syn
                  AND oi.ProductID IN {ph}
                  AND o.Created < :asof
                  AND o.Created >= DATEADD(day, -:days, :asof)
                  AND o.Created >= :history_start
            """,
            params,
        )
        for r in rows:
            by_pid.setdefault(int(r["pid"]), []).append(_as_decimal(r["price"]))
    return {pid: _rounded_float(median(v)) for pid, v in by_pid.items() if v}


def cheapest_alt_eur(
    product_ids: list[int], as_of: str, history_days: int = _HISTORY_DAYS
) -> dict[int, dict]:
    """Per product, the producer with the lowest median EUR cost (cross-supplier)."""
    if not product_ids:
        return {}
    pair: dict[tuple[int, int], list[Decimal]] = {}
    for r in _fetch_cost_rows(product_ids, as_of, history_days, producer_id=None):
        c = r["cost_eur"]
        if c is None or _as_decimal(c) <= 0:
            continue
        pair.setdefault((int(r["pid"]), int(r["producer_id"])), []).append(_as_decimal(c))
    best: dict[int, tuple[int, Decimal]] = {}
    for (pid, producer), costs in pair.items():
        m = median(costs)
        cur = best.get(pid)
        if cur is None or m < cur[1]:
            best[pid] = (producer, m)
    return {
        pid: {"producer_id": pr, "cost_eur": _rounded_float(cost)}
        for pid, (pr, cost) in best.items()
    }
