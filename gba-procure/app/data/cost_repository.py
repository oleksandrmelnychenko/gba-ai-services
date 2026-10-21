"""Verified current receipt-cost history and separately labelled recorded sale prices.

Supplier costs use the native immutable-proof endpoint. Historical sale values
remain tax-basis unverified and must never be compared as proven net margin.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from statistics import median

from app.core.history import history_start_iso
from app.core.logging import get_logger
from app.data.db import in_clause, query
from app.data.purchase_cost_cohort import PurchaseCostCohort, read_purchase_cost_cohort
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


def _cohort_for(product_ids: list[int], as_of: str, history_days: int,
                cohort: PurchaseCostCohort | None) -> PurchaseCostCohort:
    result = cohort or read_purchase_cost_cohort(product_ids, as_of, history_days)
    result.require_scope(product_ids, as_of, history_days)
    return result


def _fetch_cost_rows(
    product_ids: list[int], as_of: str, history_days: int, producer_id: int | None = None,
    *, cohort: PurchaseCostCohort | None = None,
) -> list[dict]:
    """One known observation once, including zero; no mutable currency conversion."""
    if not product_ids:
        return []
    current = _cohort_for(product_ids, as_of, history_days, cohort)
    selected = set(product_ids)
    return [{"pid": row.product_id, "producer_id": row.supplier_id,
             "observation_id": row.observation_id, "cost_eur": row.unit_net_cost_eur}
            for snapshot in current.snapshots for row in snapshot.observations
            if row.product_id in selected and row.status == "known"
            and (producer_id is None or row.supplier_id == producer_id)]


def producer_unit_costs_eur(
    producer_id: int, product_ids: list[int], as_of: str, history_days: int = _HISTORY_DAYS,
    *, cohort: PurchaseCostCohort | None = None,
) -> dict[int, float]:
    """Visible partial/complete known median; eligibility is separate provenance."""
    if not product_ids:
        return {}
    current = _cohort_for(product_ids, as_of, history_days, cohort)
    return {pid: group.median_float for pid in product_ids
            if (group := current.group(pid, producer_id)).median_float is not None}


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
    product_ids: list[int], as_of: str, history_days: int = _HISTORY_DAYS,
    *, cohort: PurchaseCostCohort | None = None,
) -> dict[int, dict]:
    """Lower historical net-goods medians; never an available current supplier offer."""
    if not product_ids:
        return {}
    current = _cohort_for(product_ids, as_of, history_days, cohort)
    result = {}
    for product in product_ids:
        usable = [group for group in current.supplier_groups(product) if group.median_float is not None]
        if usable:
            best = min(usable, key=lambda group: (group.median_unit_net_cost_eur, group.supplier_id))
            result[product] = {"producer_id": best.supplier_id, "cost_eur": best.median_float}
    return result
