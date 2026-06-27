"""Multi-producer backtest SWEEP harness — aggregates backtest_producer across a fixed PANEL.

Emits ONE json line (the A/B contract). All policy/forecast params are read from
get_settings(), so env-var overrides (env > .env) change behavior cleanly. get_settings
is lru_cached per-process, which is fine for one-shot script runs.

Usage:
  .venv/bin/python scripts/procure_backtest_sweep.py
  .venv/bin/python scripts/procure_backtest_sweep.py --gate
Overrides (optional):
  PROCURE_PRODUCERS="410430,410511"  PROCURE_AS_OF="2025-06-01,2025-09-01"  PROCURE_WINDOW=60
A/B example:
  service_level=0.95 .venv/bin/python scripts/procure_backtest_sweep.py
"""
from __future__ import annotations

import argparse
import json
import os
import sys

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
DEFAULT_GATE_PATH = os.path.join(os.path.dirname(__file__), "procure_backtest_gate.json")


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


def _empty_summary() -> dict:
    return {
        "schema_version": 1,
        "economic_cost_eur": 0.0,
        "economic_cost_eur_per_product": 0.0,
        "fill_rate": 0.0,
        "stockout_rate": 0.0,
        "overstock_units_total": 0.0,
        "overstock_units_per_product": 0.0,
        "overstock_units_per_demand": 0.0,
        "understock_margin_loss_eur": 0.0,
        "overstock_holding_cost_eur": 0.0,
        "critical_items": 0,
        "total_items": 0,
        "critical_share": 0.0,
        "n_pairs": 0,
        "n_skipped": 0,
        "products": 0,
    }


def _run_sweep(producers: list[int], as_ofs: list[str], window: int) -> dict:
    get_settings()  # ensure settings (and any env overrides) load before the sweep

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
    critical_items_total = 0
    total_items_total = 0

    for producer_id in producers:
        for as_of in as_ofs:
            try:
                r = backtest_producer(producer_id, as_of, window)
            except Exception:
                n_skipped += 1
                continue
            if r.products <= 0:
                n_skipped += 1
                continue
            n_pairs += 1
            products_total += r.products
            # weight per-pair means by products so the panel mean is product-weighted
            fill_sum += r.fill_rate * r.products
            stockout_sum += r.stockout_rate * r.products
            overstock_units_total += r.overstock_units
            # reconstruct realized demand units for per-demand normalization
            if r.overstock_units_per_demand > 0:
                demand_units_total += r.overstock_units / r.overstock_units_per_demand
            economic_cost_total += r.economic_cost_eur
            understock_loss_total += r.understock_margin_loss_eur
            overstock_hold_total += r.overstock_holding_cost_eur
            critical_items_total += r.critical_items
            total_items_total += r.total_items

    denom = max(products_total, 1)
    out = _empty_summary()
    out.update({
        "economic_cost_eur": round(economic_cost_total, 2),
        "economic_cost_eur_per_product": round(economic_cost_total / denom, 4),
        "fill_rate": round(fill_sum / denom, 4),
        "stockout_rate": round(stockout_sum / denom, 4),
        "overstock_units_total": round(overstock_units_total, 2),
        "overstock_units_per_product": round(overstock_units_total / denom, 4),
        "overstock_units_per_demand": (
            round(overstock_units_total / demand_units_total, 4) if demand_units_total > 0 else 0.0
        ),
        "understock_margin_loss_eur": round(understock_loss_total, 2),
        "overstock_holding_cost_eur": round(overstock_hold_total, 2),
        "critical_items": critical_items_total,
        "total_items": total_items_total,
        "critical_share": (
            round(critical_items_total / total_items_total, 4) if total_items_total > 0 else 0.0
        ),
        "n_pairs": n_pairs,
        "n_skipped": n_skipped,
        "products": products_total,
    })
    return out


def _load_gate(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def evaluate_gate(summary: dict, gate: dict) -> dict:
    checks = []

    def add(name: str, ok: bool, actual, expected) -> None:
        checks.append({"name": name, "ok": ok, "actual": actual, "expected": expected})

    min_pairs = gate.get("min_n_pairs")
    if min_pairs is not None:
        add("min_n_pairs", summary["n_pairs"] >= min_pairs, summary["n_pairs"], f">= {min_pairs}")

    min_products = gate.get("min_products")
    if min_products is not None:
        add("min_products", summary["products"] >= min_products, summary["products"], f">= {min_products}")

    max_critical = gate.get("max_critical_share")
    if max_critical is not None:
        add(
            "max_critical_share",
            summary["critical_share"] <= max_critical,
            summary["critical_share"],
            f"<= {max_critical}",
        )

    max_economic = gate.get("max_economic_cost_eur_per_product")
    if max_economic is not None:
        add(
            "max_economic_cost_eur_per_product",
            summary["economic_cost_eur_per_product"] <= max_economic,
            summary["economic_cost_eur_per_product"],
            f"<= {max_economic}",
        )

    max_stockout = gate.get("max_stockout_rate")
    if max_stockout is not None:
        add(
            "max_stockout_rate",
            summary["stockout_rate"] <= max_stockout,
            summary["stockout_rate"],
            f"<= {max_stockout}",
        )

    return {"ok": all(c["ok"] for c in checks), "checks": checks, "summary": summary}


def main() -> None:
    ap = argparse.ArgumentParser(description="Procurement multi-producer backtest sweep.")
    ap.add_argument("--gate", action="store_true", help="evaluate the committed gate thresholds")
    ap.add_argument("--gate-file", default=DEFAULT_GATE_PATH)
    args = ap.parse_args()

    producers, as_ofs, window = _panel()
    summary = _run_sweep(producers, as_ofs, window)
    if not args.gate:
        print(json.dumps(summary))
        return

    report = evaluate_gate(summary, _load_gate(args.gate_file))
    print(json.dumps(report))
    if not report["ok"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
