"""Rolling-origin holdout: WAPE and bias per demand class for the service and candidates.

Usage:
    .venv/bin/python scripts/holdout_backtest.py --psv fc_series.psv --last-month 2026-10
    .venv/bin/python scripts/holdout_backtest.py --from-db [--as-of YYYY-MM-DD]

The PSV rows are kind|entity_id|yyyy-MM|eur|qty with kind C (clients) or P (products).
--from-db reads the same closed months the live service trains on and prints the refresh gate.
"""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.core.config import get_settings
from app.services.forecasting import accuracy_gate, holdout


def month_range(first: str, last: str) -> list[str]:
    year, month = (int(part) for part in first.split("-"))
    labels: list[str] = []
    while f"{year:04d}-{month:02d}" <= last:
        labels.append(f"{year:04d}-{month:02d}")
        month += 1
        if month == 13:
            year, month = year + 1, 1
    return labels


def load_psv(path: str, labels: list[str]) -> dict[str, dict[int, dict[str, float]]]:
    allowed = set(labels)
    data: dict[str, dict[int, dict[str, float]]] = {"C": {}, "P": {}}
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            parts = line.strip().split("|")
            if len(parts) < 4 or parts[0] not in data or parts[2] not in allowed:
                continue
            data[parts[0]].setdefault(int(parts[1]), {})[parts[2]] = float(parts[3])
    return data


def print_table(title: str, evaluation: dict) -> None:
    print(
        f"\n== {title}: scored points={evaluation['scored_points']}, "
        f"refused share of actual EUR={evaluation['refused_actual_share_pct']}%"
    )
    for group, block in evaluation["by_class"].items():
        print(f"  {group:<13} points={block['points']:>6} actual_eur={block['actual_eur']:>12.0f}")
        ranked = sorted(block["methods"].items(), key=lambda item: item[1]["wape_pct"])
        for name, metrics in ranked:
            print(f"      {name:<14} WAPE {metrics['wape_pct']:>7.1f}%  bias {metrics['bias_pct']:>+7.1f}%")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--psv")
    source.add_argument("--from-db", action="store_true")
    parser.add_argument("--first-month", default=None)
    parser.add_argument("--last-month", default=None)
    parser.add_argument("--as-of", default=None)
    parser.add_argument("--origins", type=int, default=6)
    parser.add_argument("--lead", type=int, default=accuracy_gate.SERVED_LEAD_MONTHS)
    parser.add_argument("--all-candidates", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    cfg = get_settings()
    if args.from_db:
        as_of = args.as_of or datetime.now(ZoneInfo("Europe/Kyiv")).date().isoformat()
        result = accuracy_gate.run(as_of, args.origins)
        if args.json:
            print(json.dumps(result, sort_keys=True))
            return
        print(f"status={result['status']}")
        for scope in ("clients", "products"):
            if scope in result:
                print_table(scope, result[scope])
        return

    first = args.first_month or cfg.source_history_start_date.strftime("%Y-%m")
    last = args.last_month or date.today().strftime("%Y-%m")
    labels = month_range(first, last)
    methods = list(holdout.CANDIDATES) if args.all_candidates else list(holdout.GATE_METHODS)
    data = load_psv(args.psv, labels)
    output: dict = {}
    for kind, scope in (("C", "clients"), ("P", "products")):
        output[scope] = holdout.evaluate(
            accuracy_gate.dense_closed_series(data[kind], labels),
            origins=args.origins,
            min_non_zero=cfg.min_history_months,
            methods=methods,
            scope=accuracy_gate.SCOPE_BY_GATE[scope],
            lead=args.lead,
        )
    if args.json:
        print(json.dumps(output, sort_keys=True))
        return
    for scope, evaluation in output.items():
        print_table(scope, evaluation)


if __name__ == "__main__":
    main()
