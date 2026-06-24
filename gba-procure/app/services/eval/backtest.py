"""Replenishment backtest — does the policy prevent stockouts without over-ordering?

Protocol (point-in-time, per product for a producer):
  1. At a historical as_of, compute inventory position + the policy's suggested_qty.
  2. Look at ACTUAL demand over the next `eval_window` days (from Order/OrderItem > as_of).
  3. Outcomes (fill/stockout are unit/event based; overstock is MAGNITUDE based):
     - fill:            min(position + suggested_qty, demand) / demand              (unit-based)
     - stockout (event): position + suggested_qty < demand                          (under-ordered)
     - overstock_units: max(0, (position + suggested_qty) - demand)                 (EXCESS UNITS)

OVERSTOCK is the count of EXCESS UNITS the post-order position carries beyond realized
forward demand: max(0, order_up_to - forward_demand) when ordering (suggested_qty>0), where
order_up_to = position + suggested_qty; max(0, position - forward_demand) carry when idle.
Equivalently sum(max(0, (position + suggested_qty) - forward_demand)). This is the policy's
controllable excess — raising service_level inflates suggested_qty (more safety stock), so
overstock_units grows continuously and can be traded off against the (unit) stockout shortfall
on the SAME magnitude scale. The old binary `suggested_qty > demand*factor` flag saturated and
could not measure that trade-off; it is kept only as a legacy diagnostic (overstock_rate).

Reported overstock is normalized two ways:
  - overstock_units_per_product = total excess units / products_evaluated
  - overstock_units_per_demand  = total excess units / total realized demand units

NOTE: forecast & policy read history < as_of; actual demand reads > as_of → eval-safe.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.core.config import get_settings
from app.data.db import in_clause, query
from app.services.replenishment import policy


@dataclass
class BacktestResult:
    producer_id: int
    as_of: str
    eval_window_days: int
    products: int
    fill_rate: float          # mean fraction of actual demand covered by position+suggestion
    stockout_rate: float      # share of products that would still stock out (event)
    overstock_units: float    # total EXCESS units beyond realized forward demand (magnitude)
    overstock_units_per_product: float  # excess units / products evaluated
    overstock_units_per_demand: float   # excess units / total realized demand units
    overstock_rate: float     # LEGACY binary flag: share ordered far above realized demand
    coverage_with_policy: float   # share fully covered WITH the suggestion
    coverage_without_policy: float  # share fully covered WITHOUT it (counterfactual)
    economic_cost_eur: float  # margin-weighted: lost-margin (understock) + holding (overstock)
    understock_margin_loss_eur: float
    overstock_holding_cost_eur: float


def _actual_demand(product_ids: list[int], as_of: str, window_days: int) -> dict[int, float]:
    if not product_ids:
        return {}
    ph, params = in_clause("p", product_ids)
    rows = query(
        f"""
        SELECT oi.ProductID AS pid, SUM(oi.Qty) AS units
        FROM dbo.[Order] o
        JOIN dbo.OrderItem oi ON oi.OrderID = o.ID
        WHERE o.Deleted = 0
              AND o.Created >= :asof
              AND o.Created < DATEADD(day, :win, :asof)
              AND oi.ProductID IN {ph}
        GROUP BY oi.ProductID
        """,
        {"asof": as_of, "win": window_days, **params},
    )
    return {int(r["pid"]): float(r["units"] or 0) for r in rows}


def backtest_producer(producer_id: int, as_of: str, eval_window_days: int | None = None,
                      overstock_factor: float = 3.0) -> BacktestResult:
    s = get_settings()
    window = eval_window_days or s.forecast_horizon_days

    plan = policy.build_plan(producer_id, as_of, only_needed=False)
    product_ids = [it.product_id for it in plan.items]
    actual = _actual_demand(product_ids, as_of, window)

    s = get_settings()
    holding_per_unit_cost = (s.holding_rate_annual / 365.0) * window

    n = covered_with = covered_without = stockouts = overstocks = 0
    fill_sum = 0.0
    overstock_units = 0.0      # magnitude-aware excess across ALL items (demand>0 and =0)
    demand_units = 0.0
    understock_loss = overstock_hold = 0.0
    for it in plan.items:
        demand = actual.get(it.product_id, 0.0)
        position = it.inventory.position
        with_policy = position + it.suggested_qty
        # EXCESS UNITS carried beyond realized forward demand (the policy's controllable
        # overstock). with_policy == order_up_to when ordering; == position when idle.
        excess = max(0.0, with_policy - demand)
        short = max(0.0, demand - with_policy)
        overstock_units += excess
        if it.unit_margin_eur is not None:
            understock_loss += short * max(0.0, it.unit_margin_eur)
        if it.unit_cost_eur is not None:
            overstock_hold += excess * holding_per_unit_cost * it.unit_cost_eur
        # LEGACY binary diagnostic (saturating; kept for continuity, not for the trade-off).
        if it.suggested_qty > demand * overstock_factor:
            overstocks += 1
        if demand <= 0:
            continue
        n += 1
        demand_units += demand
        fill_sum += min(with_policy, demand) / demand
        if position >= demand:
            covered_without += 1
        if with_policy >= demand:
            covered_with += 1
        else:
            stockouts += 1

    denom = max(n, 1)
    total_items = max(len(plan.items), 1)
    return BacktestResult(
        producer_id=producer_id, as_of=as_of, eval_window_days=window,
        products=n,
        fill_rate=round(fill_sum / denom, 3),
        stockout_rate=round(stockouts / denom, 3),
        overstock_units=round(overstock_units, 2),
        overstock_units_per_product=round(overstock_units / total_items, 3),
        overstock_units_per_demand=round(overstock_units / demand_units, 3) if demand_units > 0 else 0.0,
        overstock_rate=round(overstocks / total_items, 3),
        coverage_with_policy=round(covered_with / denom, 3),
        coverage_without_policy=round(covered_without / denom, 3),
        economic_cost_eur=round(understock_loss + overstock_hold, 2),
        understock_margin_loss_eur=round(understock_loss, 2),
        overstock_holding_cost_eur=round(overstock_hold, 2),
    )


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--producer", type=int, required=True)
    ap.add_argument("--as-of", required=True, help="historical date YYYY-MM-DD (need future demand after it)")
    ap.add_argument("--window", type=int, default=None)
    args = ap.parse_args()
    r = backtest_producer(args.producer, args.as_of, args.window)
    print(f"Producer {r.producer_id} @ {r.as_of}, window={r.eval_window_days}d, "
          f"products_with_demand={r.products}")
    print(f"  fill_rate           : {r.fill_rate:.3f}  (demand covered by position+suggestion)")
    print(f"  coverage WITH policy : {r.coverage_with_policy:.3f}")
    print(f"  coverage WITHOUT     : {r.coverage_without_policy:.3f}  (counterfactual)")
    print(f"  stockout_rate        : {r.stockout_rate:.3f}  (still short after ordering)")
    print(f"  overstock_units      : {r.overstock_units:.1f}  (excess units beyond realized demand)")
    print(f"  overstock_units/prod : {r.overstock_units_per_product:.3f}")
    print(f"  overstock_units/dmd  : {r.overstock_units_per_demand:.3f}")
    print(f"  overstock_rate(legacy): {r.overstock_rate:.3f}  (binary: ordered >> realized demand)")
    print(f"  economic_cost_eur    : {r.economic_cost_eur:.2f}  "
          f"(understock_margin_loss {r.understock_margin_loss_eur:.2f} + "
          f"overstock_holding {r.overstock_holding_cost_eur:.2f})")


if __name__ == "__main__":
    main()
