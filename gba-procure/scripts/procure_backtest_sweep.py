"""Multi-producer backtest SWEEP harness — aggregates backtest_producer across a fixed PANEL.

Emits ONE json line (the A/B contract). All policy/forecast params are read from
get_settings(), so env-var overrides (env > .env) change behavior cleanly. get_settings
is lru_cached per-process, which is fine for one-shot script runs.

Usage:
  .venv/bin/python scripts/procure_backtest_sweep.py
Overrides (optional):
  PROCURE_PRODUCERS="410430,410511"  PROCURE_AS_OF="2025-06-01,2025-09-01"  PROCURE_WINDOW=60
A/B example:
  service_level=0.95 .venv/bin/python scripts/procure_backtest_sweep.py
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.config import get_settings  # noqa: E402
from app.services.eval.backtest import backtest_producer  # noqa: E402

PRODUCER_IDS = [
    410430, 410511, 410552, 410579, 410580, 410617, 410661, 410719, 410727,
    410817, 410988, 411075, 411080, 411108, 411116, 411243, 411248, 411270,
    411448, 411503, 411673, 411894, 411927, 412029, 412071, 412075, 412082,
    412083, 412179, 414625,
]
AS_OF_DATES = ["2025-06-01", "2025-09-01", "2025-12-01"]
EVAL_WINDOW_DAYS = 60


def _panel() -> tuple[list[int], list[str], int]:
    producers = PRODUCER_IDS
    as_ofs = AS_OF_DATES
    window = EVAL_WINDOW_DAYS
    if os.environ.get("PROCURE_PRODUCERS"):
        producers = [int(x) for x in os.environ["PROCURE_PRODUCERS"].split(",") if x.strip()]
    if os.environ.get("PROCURE_AS_OF"):
        as_ofs = [x.strip() for x in os.environ["PROCURE_AS_OF"].split(",") if x.strip()]
    if os.environ.get("PROCURE_WINDOW"):
        window = int(os.environ["PROCURE_WINDOW"])
    return producers, as_ofs, window


def main() -> None:
    get_settings()  # ensure settings (and any env overrides) load before the sweep
    producers, as_ofs, window = _panel()

    n_pairs = 0          # successfully evaluated (producer, as_of) pairs with >0 products
    n_skipped = 0        # errored or zero-product pairs
    products_total = 0
    fill_sum = 0.0
    stockout_sum = 0.0
    overstock_units_total = 0.0
    demand_units_total = 0.0
    economic_cost_total = 0.0
    understock_loss_total = 0.0
    overstock_hold_total = 0.0
    errors: list[dict[str, Any]] = []

    for producer_id in producers:
        for as_of in as_ofs:
            try:
                r = backtest_producer(producer_id, as_of, window)
            except Exception as exc:  # noqa: BLE001
                n_skipped += 1
                errors.append({"producer_id": producer_id, "as_of": as_of, "error": str(exc)})
                continue
            if r.products <= 0:
                n_skipped += 1
                errors.append(
                    {"producer_id": producer_id, "as_of": as_of, "error": "no_products_with_demand"}
                )
                continue
            n_pairs += 1
            products_total += r.products
            # weight per-pair means by products so the panel mean is product-weighted
            fill_sum += r.fill_rate * r.products
            stockout_sum += r.stockout_rate * r.products
            overstock_units_total += r.overstock_units
            demand_units_total += r.demand_units
            economic_cost_total += r.economic_cost_eur
            understock_loss_total += r.understock_margin_loss_eur
            overstock_hold_total += r.overstock_holding_cost_eur

    denom = max(products_total, 1)
    out = {
        "economic_cost_eur": round(economic_cost_total, 2),
        "fill_rate": round(fill_sum / denom, 4),
        "stockout_rate": round(stockout_sum / denom, 4),
        "overstock_units_total": round(overstock_units_total, 2),
        "overstock_units_per_product": round(overstock_units_total / denom, 4),
        "overstock_units_per_demand": (
            round(overstock_units_total / demand_units_total, 4) if demand_units_total > 0 else 0.0
        ),
        "understock_margin_loss_eur": round(understock_loss_total, 2),
        "overstock_holding_cost_eur": round(overstock_hold_total, 2),
        "n_pairs": n_pairs,
        "n_skipped": n_skipped,
        "products": products_total,
        "errors": errors[:10],
    }
    print(json.dumps(out))
    if n_pairs <= 0:
        raise SystemExit("procure_backtest_sweep_no_successful_pairs")


if __name__ == "__main__":
    main()
