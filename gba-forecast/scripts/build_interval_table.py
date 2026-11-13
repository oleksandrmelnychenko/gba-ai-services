"""Build the prediction-interval table from rolling-origin relative errors.

Usage:
    .venv/bin/python -m scripts.build_interval_table --psv series.psv --last-month 2026-10 \
        --table-version 2026-11-13

The PSV rows are kind|entity_id|yyyy-MM|eur with kind C (client), P (product) or X (client+product
pair). For every series and every origin the live selection forecasts steps 2..7 past the last
training month; the ratio actual / forecast is collected per scope, demand class, forecast-size
band and step. The table stores the 10th and 90th percentile of that ratio. Coverage is checked
out of sample: percentiles from the earlier targets, hit rate on the later ones. Where the later
targets exceed the upper bound more often than the nominal 10% at a step, the upper bound of
that scope and step is widened by the smallest factor that restores 10%, never less than at
the step before.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

from app.core.config import get_settings
from app.services.forecasting import classify, intervals, selection
from scripts.holdout_backtest import month_range

KIND_SCOPE = {"C": selection.SCOPE_CLIENT, "P": selection.SCOPE_PRODUCT, "X": selection.SCOPE_PAIR}
CLASSES = (classify.SMOOTH, classify.ERRATIC, classify.INTERMITTENT, classify.LUMPY)
MIN_TRAIN_MONTHS = 6
MIN_CELL = 200
BANDS = 3
STEPS = tuple(range(intervals.FIRST_STEP, intervals.LAST_STEP + 1))
WIDENING_GRID = tuple(round(1.0 + 0.05 * i, 2) for i in range(41))
NOMINAL_ABOVE = round(1.0 - intervals.UPPER_QUANTILE, 6)


def quantile(sorted_values: list[float], q: float) -> float:
    if not sorted_values:
        raise ValueError("quantile of an empty sample")
    position = q * (len(sorted_values) - 1)
    low = int(position)
    high = min(low + 1, len(sorted_values) - 1)
    return sorted_values[low] + (sorted_values[high] - sorted_values[low]) * (position - low)


def load(path: str, labels: list[str]) -> dict[str, dict[str, list[float]]]:
    index = {label: i for i, label in enumerate(labels)}
    data: dict[str, dict[str, list[float]]] = {scope: {} for scope in KIND_SCOPE.values()}
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            parts = line.rstrip("\n").split("|")
            if len(parts) < 4 or parts[0] not in KIND_SCOPE or parts[2] not in index:
                continue
            series = data[KIND_SCOPE[parts[0]]].setdefault(parts[1], [0.0] * len(labels))
            series[index[parts[2]]] += float(parts[3])
    return data


def records(series_by_id: dict[str, list[float]], scope: str, min_non_zero: int) -> list[tuple]:
    out: list[tuple] = []
    steps = range(intervals.FIRST_STEP, intervals.LAST_STEP + 1)
    for series in series_by_id.values():
        for origin in range(MIN_TRAIN_MONTHS, len(series)):
            train = series[:origin]
            if sum(1 for value in train if value > 0) < min_non_zero:
                continue
            segment = classify.classify(train)
            forecast = selection.auto_rate(train, segment, scope)[0]
            if forecast <= 0:
                continue
            for step in steps:
                target = origin - 1 + step
                if target >= len(series):
                    break
                out.append((segment, step, target, forecast, series[target] / forecast))
    return out


def fit(rows: list[tuple]) -> dict:
    by_class: dict[str, list[tuple]] = defaultdict(list)
    for row in rows:
        by_class[row[0]].append(row)
    table: dict[str, dict] = {}
    for segment in CLASSES:
        sample = by_class.get(segment, [])
        if len(sample) < MIN_CELL:
            continue
        forecasts = sorted(row[3] for row in sample)
        edges = [round(quantile(forecasts, i / BANDS), 2) for i in range(1, BANDS)]
        bands = []
        for band in range(BANDS):
            in_band = [row for row in sample if intervals.band_index(edges, row[3]) == band]
            pooled = sorted(row[4] for row in in_band)
            cells = []
            for step in range(intervals.FIRST_STEP, intervals.LAST_STEP + 1):
                ratios = sorted(row[4] for row in in_band if row[1] == step)
                source = ratios if len(ratios) >= MIN_CELL else pooled
                cells.append(
                    [
                        round(quantile(source, intervals.LOWER_QUANTILE), 3),
                        round(quantile(source, intervals.UPPER_QUANTILE), 3),
                        len(ratios),
                    ]
                )
            bands.append(cells)
        table[segment] = {"size_edges_eur": edges, "bands": bands}
    return table


def widened(table: dict, factors: dict[int, float]) -> dict:
    out: dict[str, dict] = {}
    for segment, entry in table.items():
        bands = [
            [
                [low, round(1.0 + (max(high, 1.0) - 1.0) * factors.get(step, 1.0), 3), count]
                for step, (low, high, count) in zip(STEPS, cells, strict=True)
            ]
            for cells in entry["bands"]
        ]
        out[segment] = {"size_edges_eur": entry["size_edges_eur"], "bands": bands}
    return out


def above_share(table: dict, rows: list[tuple], factor: float) -> float:
    total = above = 0
    for segment, step, _target, forecast, ratio in rows:
        if segment not in table:
            continue
        _low, high = intervals.lookup(table, segment, forecast, step)
        total += 1
        above += ratio > 1.0 + (max(high, 1.0) - 1.0) * factor
    return above / total if total else 0.0


def upper_widening(table: dict, rows: list[tuple]) -> dict[int, float]:
    factors: dict[int, float] = {}
    floor = 1.0
    for step in STEPS:
        at_step = [row for row in rows if row[1] == step]
        allowed = [factor for factor in WIDENING_GRID if factor >= floor]
        floor = next(
            (factor for factor in allowed if above_share(table, at_step, factor) <= NOMINAL_ABOVE),
            allowed[-1],
        )
        factors[step] = floor
    return factors


def coverage(table: dict, rows: list[tuple], by_step: bool = False) -> dict[str, dict]:
    hits: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])
    for segment, step, _target, forecast, ratio in rows:
        if segment not in table:
            continue
        low, high = intervals.lookup(table, segment, forecast, step)
        cell = hits[str(step) if by_step else segment]
        cell[0] += 1
        cell[1] += low <= ratio <= high
        cell[2] += ratio > high
    return {
        key: {
            "points": cell[0],
            "inside_pct": round(100.0 * cell[1] / cell[0], 1),
            "above_pct": round(100.0 * cell[2] / cell[0], 1),
        }
        for key, cell in sorted(hits.items())
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--psv", required=True)
    parser.add_argument("--first-month", default=None)
    parser.add_argument("--last-month", required=True)
    parser.add_argument("--table-version", required=True)
    parser.add_argument("--out", default=str(intervals.TABLE_PATH))
    args = parser.parse_args()

    cfg = get_settings()
    first = args.first_month or cfg.source_history_start_date.strftime("%Y-%m")
    labels = month_range(first, args.last_month)
    data = load(args.psv, labels)
    scopes: dict[str, dict] = {}
    validation: dict[str, dict] = {}
    counts: dict[str, int] = {}
    widening: dict[str, dict[str, float]] = {}
    for scope, series_by_id in data.items():
        rows = records(series_by_id, scope, cfg.min_history_months)
        counts[scope] = len(rows)
        targets = sorted({row[2] for row in rows})
        cut = targets[int(len(targets) * 0.6)]
        early = fit([row for row in rows if row[2] < cut])
        late = [row for row in rows if row[2] >= cut]
        factors = upper_widening(early, late)
        widening[scope] = {str(step): factor for step, factor in factors.items()}
        scopes[scope] = widened(fit(rows), factors)
        validation[scope] = {
            "test_from_month": labels[cut],
            "by_class": coverage(widened(early, factors), late),
            "by_step": coverage(widened(early, factors), late, by_step=True),
            "by_step_before_widening": coverage(early, late, by_step=True),
        }
    payload = {
        "table_version": args.table_version,
        "lower_quantile": intervals.LOWER_QUANTILE,
        "upper_quantile": intervals.UPPER_QUANTILE,
        "first_step": intervals.FIRST_STEP,
        "last_step": intervals.LAST_STEP,
        "source": {
            "first_month": labels[0],
            "last_month": labels[-1],
            "min_train_months": MIN_TRAIN_MONTHS,
            "records": counts,
        },
        "upper_widening": widening,
        "out_of_sample": validation,
        "scopes": scopes,
    }
    Path(args.out).write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {"records": counts, "upper_widening": widening, "out_of_sample": validation},
            indent=1,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
