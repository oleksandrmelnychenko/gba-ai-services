#!/usr/bin/env python3
"""Backtest product health against future demand and margin outcomes.

Example:
  python scripts/product_health_backtest.py --as-of 2025-12-01 --horizon-days 180 --write-baseline
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_BASELINE = ROOT / "docs" / "product-health-backtest-baseline.json"
GATED_CORRELATIONS = [
    "spearman_health_to_future_revenue",
    "spearman_health_to_future_units",
    "spearman_health_to_future_margin",
    "spearman_demand_score_to_future_revenue",
    "spearman_demand_score_to_future_units",
    "spearman_margin_score_to_future_margin",
]


def _parse_day(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def _future_outcomes(
    as_of: date,
    horizon_days: int,
    snapshot_rows: list[dict[str, Any]],
) -> dict[int, dict[str, Any]]:
    from app.data import signals_repository as sig

    end = as_of + timedelta(days=horizon_days)
    snapshot_by_pid = {int(r["product_id"]): r for r in snapshot_rows}

    sold = {
        int(r["product_id"]): float(r["sold_qty"] or 0.0)
        for r in sig.sales_velocity(end.isoformat(), horizon_days)
    }
    price = {
        int(r["product_id"]): float(r["avg_price_eur"] or 0.0)
        for r in sig.avg_sale_price_eur(end.isoformat(), horizon_days)
    }
    returns = {
        int(r["product_id"]): r
        for r in sig.returns_for_products(end.isoformat(), horizon_days)
    }

    outcomes: dict[int, dict[str, Any]] = {}
    for pid, snap in snapshot_by_pid.items():
        units = sold.get(pid, 0.0)
        avg_price = price.get(pid, 0.0)
        returned_units = float(returns.get(pid, {}).get("returned_qty") or 0.0)
        returned_value = float(returns.get(pid, {}).get("returned_value_eur") or 0.0)
        revenue = units * avg_price
        net_revenue = max(0.0, revenue - returned_value)
        unit_cost = snap.get("unit_cost_eur")
        margin = None
        if unit_cost is not None:
            margin = net_revenue - max(0.0, units - returned_units) * float(unit_cost)
        outcomes[pid] = {
            "future_units": units,
            "future_revenue_eur": round(revenue, 2),
            "future_returned_units": returned_units,
            "future_returned_value_eur": round(returned_value, 2),
            "future_margin_eur": round(margin, 2) if margin is not None else None,
        }
    return outcomes


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    return json.loads(path.read_text())


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _check_regression(current: dict[str, Any], baseline: dict[str, Any], tolerance: float) -> list[str]:
    failures: list[str] = []
    for key in GATED_CORRELATIONS:
        cur = current.get("summary", {}).get("correlations", {}).get(key)
        old = baseline.get("summary", {}).get("correlations", {}).get(key)
        if cur is None:
            failures.append(f"{key} missing in current report")
            continue
        if old is None:
            failures.append(f"{key} missing in baseline report")
            continue
        if cur < old - tolerance:
            failures.append(f"{key} regressed: current={cur}, baseline={old}, tolerance={tolerance}")
    failures.extend(_check_by_action_shape(current, "current"))
    failures.extend(_check_by_action_shape(baseline, "baseline"))
    return failures


def _check_by_action_shape(payload: dict[str, Any], label: str) -> list[str]:
    summary = payload.get("summary", {})
    expected_n = summary.get("n")
    by_action = summary.get("by_action")
    if not isinstance(expected_n, int):
        return [f"{label} summary.n missing"]
    if not isinstance(by_action, list):
        return [f"{label} summary.by_action missing"]
    total = 0
    failures: list[str] = []
    for idx, row in enumerate(by_action):
        if not isinstance(row, dict):
            failures.append(f"{label} by_action[{idx}] is not an object")
            continue
        if not row.get("action_label"):
            failures.append(f"{label} by_action[{idx}].action_label missing")
        n = row.get("n")
        if not isinstance(n, int):
            failures.append(f"{label} by_action[{idx}].n missing")
            continue
        total += n
    if total != expected_n:
        failures.append(f"{label} by_action count mismatch: total={total}, summary.n={expected_n}")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", required=True, help="Snapshot date YYYY-MM-DD.")
    parser.add_argument("--horizon-days", type=int, default=180)
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--write-baseline", action="store_true")
    parser.add_argument("--fail-on-regression", action="store_true")
    parser.add_argument("--regression-tolerance", type=float, default=0.02)
    args = parser.parse_args()

    from app.services import portfolio
    from app.services.health_backtest import evaluate_health_snapshot

    as_of = _parse_day(args.as_of)
    build = portfolio.build_portfolio(as_of.isoformat())
    rows = build["rows"]
    outcomes = _future_outcomes(as_of, args.horizon_days, rows)
    summary = evaluate_health_snapshot(rows, outcomes)
    payload = {
        "as_of": as_of.isoformat(),
        "outcome_end": (as_of + timedelta(days=args.horizon_days)).isoformat(),
        "horizon_days": args.horizon_days,
        "model_version": build["model_version"],
        "summary": summary,
    }

    print(json.dumps(payload, indent=2, sort_keys=True))

    baseline = _load_json(args.baseline)
    if args.write_baseline:
        _write_json(args.baseline, payload)
        return 0

    if args.fail_on_regression:
        if baseline is None:
            print(f"baseline not found: {args.baseline}", file=sys.stderr)
            return 2
        failures = _check_regression(payload, baseline, args.regression_tolerance)
        if failures:
            for failure in failures:
                print(failure, file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
