"""Run the point-in-time accuracy backtest on a real DB sample and print the accuracy table.

This is the forecaster's validation harness — the analog of computing AUC for the solvency
classifier. It does NOT train or change any model: it replays what the live service would have
forecast at each past origin month and scores it against what actually happened.

Usage (read-only DB; uses the repo .env):

    .venv/bin/python scripts/run_backtest.py [--clients N] [--products N]
                                             [--horizon H] [--origins K]
                                             [--history M] [--by mae|smape] [--json]

Outputs:
  1. A per-method x per-segment accuracy table (MAE / sMAPE / bias / n).
  2. The empirically best method per segment (the selection rule).
  3. Overall MAE of the current fixed default vs the per-segment selection, with the
     error-reduction %.

With --json, emits one machine-readable summary object for committed baselines/gates.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime

from app.core.config import get_settings
from app.data import signals_repository as sig
from app.services.forecast import history_labels, series_from
from app.services.forecasting import backtest, classify, selection


def _dense(history: dict[str, float], as_of, months: int) -> list[float]:
    return series_from(history, history_labels(as_of, months))


def _collect(args) -> backtest.BacktestResult:
    cfg = get_settings()
    as_of_str = datetime.now(UTC).strftime("%Y-%m-%d")
    as_of = datetime.now(UTC).date()
    months = args.history or cfg.history_months
    result = backtest.BacktestResult()

    series_list: list[list[float]] = []
    if args.clients > 0:
        rows = sig.sample_client_monthly_series(as_of_str, months, args.clients)
        for hist in sig.group_series_by_entity(rows, "cid").values():
            series_list.append(_dense(hist, as_of, months))
    if args.products > 0:
        rows = sig.sample_product_monthly_series(as_of_str, months, args.products)
        for hist in sig.group_series_by_entity(rows, "pid").values():
            series_list.append(_dense(hist, as_of, months))

    for series in series_list:
        backtest.backtest_series(
            series,
            result,
            horizon=args.horizon,
            eval_origins=args.origins,
            min_train=cfg.min_history_months,
            alpha=cfg.croston_alpha,
        )
    return result


_SEG_ORDER = [classify.SMOOTH, classify.ERRATIC, classify.INTERMITTENT, classify.LUMPY,
              classify.NO_DEMAND]


def _acc_summary(acc: backtest.Accumulator) -> dict:
    return {
        "n": acc.n,
        "mae": round(acc.mae, 6),
        "smape": round(acc.smape, 6),
        "bias": round(acc.bias, 6),
    }


def build_summary(result: backtest.BacktestResult, by: str) -> dict:
    chosen = backtest.best_method_per_segment(result, by=by)
    fixed_maes = {
        m: backtest.overall_mae(result, m)
        for m in backtest.METHODS
        if result.by_method.get(m) and result.by_method[m].n
    }
    best_fixed_method = min(fixed_maes, key=fixed_maes.get) if fixed_maes else None
    best_fixed_mae = fixed_maes[best_fixed_method] if best_fixed_method else None
    legacy_mae = fixed_maes.get("moving_avg") if fixed_maes else None

    selection_abs_err = 0.0
    selection_n = 0
    for seg, method in chosen.items():
        acc = result.by_method_segment.get((method, seg))
        if acc and acc.n:
            selection_abs_err += acc.abs_err_sum
            selection_n += acc.n
    selection_mae = selection_abs_err / selection_n if selection_n else None

    def reduction(base: float | None) -> float | None:
        if base is None or selection_mae is None or base <= 0:
            return None
        return round((base - selection_mae) / base, 6)

    by_segment: dict[str, dict[str, dict]] = {}
    for (method, segment), acc in result.by_method_segment.items():
        if acc.n:
            by_segment.setdefault(segment, {})[method] = _acc_summary(acc)

    return {
        "schema_version": 1,
        "metric": by,
        "methods": list(backtest.METHODS),
        "n_series": result.n_series,
        "n_origins": result.n_origins,
        "segment_counts": dict(sorted(result.segment_counts.items())),
        "by_method": {
            method: _acc_summary(acc)
            for method, acc in result.by_method.items()
            if acc.n
        },
        "by_segment": by_segment,
        "selection": {
            "method": "auto",
            "best_method_per_segment": chosen,
            "legacy_method": "moving_avg",
            "legacy_mae": round(legacy_mae, 6) if legacy_mae is not None else None,
            "best_fixed_method": best_fixed_method,
            "best_fixed_mae": round(best_fixed_mae, 6) if best_fixed_mae is not None else None,
            "auto_mae": round(selection_mae, 6) if selection_mae is not None else None,
            "auto_vs_legacy_error_reduction": reduction(legacy_mae),
            "auto_vs_best_fixed_error_reduction": reduction(best_fixed_mae),
        },
    }


def _print_table(result: backtest.BacktestResult) -> None:
    print(f"\nSeries: {result.n_series}   Origins replayed: {result.n_origins}")
    print("Segment distribution:")
    for seg in _SEG_ORDER:
        if seg in result.segment_counts:
            print(f"  {seg:<13} {result.segment_counts[seg]:>4} series")

    print(f"\n{'segment':<13} {'method':<11} {'n':>6} {'MAE':>12} {'sMAPE':>8} {'bias':>12}")
    print("-" * 66)
    for seg in _SEG_ORDER:
        rows = [(m, result.by_method_segment.get((m, seg)))
                for m in backtest.METHODS]
        rows = [(m, a) for m, a in rows if a and a.n]
        if not rows:
            continue
        for m, a in rows:
            print(f"{seg:<13} {m:<11} {a.n:>6} {a.mae:>12.2f} {a.smape:>8.3f} {a.bias:>12.2f}")
        print()

    print(f"{'OVERALL':<13} {'method':<11} {'n':>6} {'MAE':>12} {'sMAPE':>8} {'bias':>12}")
    print("-" * 66)
    for m in backtest.METHODS:
        a = result.by_method.get(m)
        if a and a.n:
            print(f"{'(all)':<13} {m:<11} {a.n:>6} {a.mae:>12.2f} {a.smape:>8.3f} {a.bias:>12.2f}")


def _report_selection(result: backtest.BacktestResult, by: str) -> None:
    chosen = backtest.best_method_per_segment(result, by=by)
    print(f"\nEmpirical best method per segment (by {by}):")
    for seg in _SEG_ORDER:
        if seg in chosen:
            prior = selection.DEFAULT_SEGMENT_METHOD.get(seg, "?")
            tag = "" if chosen[seg] == prior else f"  (prior was {prior})"
            print(f"  {seg:<13} -> {chosen[seg]}{tag}")

    # Error reduction: best fixed single method vs the per-segment selection, on the same
    # forecasts. The selection MAE re-aggregates each segment's chosen-method MAE weighted by n.
    fixed_maes = {m: backtest.overall_mae(result, m) for m in backtest.METHODS
                  if result.by_method.get(m) and result.by_method[m].n}
    if not fixed_maes:
        print("\nNo scored forecasts — sample too thin.")
        return
    best_fixed_method = min(fixed_maes, key=fixed_maes.get)
    best_fixed_mae = fixed_maes[best_fixed_method]
    legacy_mae = fixed_maes.get("moving_avg", best_fixed_mae)

    sel_err = 0.0
    sel_n = 0
    for seg, method in chosen.items():
        a = result.by_method_segment.get((method, seg))
        if a and a.n:
            sel_err += a.abs_err_sum
            sel_n += a.n
    sel_mae = sel_err / sel_n if sel_n else 0.0

    def pct(base: float) -> str:
        return f"{(base - sel_mae) / base * 100:+.1f}%" if base > 0 else "n/a"

    print("\nOverall MAE (lower is better):")
    print(f"  fixed moving_avg (legacy default) : {legacy_mae:>12.2f}")
    print(f"  best fixed single method ({best_fixed_method:<10}): {best_fixed_mae:>12.2f}")
    print(f"  per-segment selection (auto)      : {sel_mae:>12.2f}")
    print(f"\n  selection vs legacy moving_avg : {pct(legacy_mae)} error reduction")
    print(f"  selection vs best fixed method : {pct(best_fixed_mae)} error reduction")


def main() -> None:
    p = argparse.ArgumentParser(description="Forecast accuracy backtest (rolling-origin).")
    p.add_argument("--clients", type=int, default=200)
    p.add_argument("--products", type=int, default=200)
    p.add_argument("--horizon", type=int, default=3)
    p.add_argument("--origins", type=int, default=12)
    p.add_argument("--history", type=int, default=0, help="0 => use HISTORY_MONTHS")
    p.add_argument("--by", choices=["mae", "smape"], default="mae")
    p.add_argument("--json", action="store_true", help="emit machine-readable JSON summary only")
    args = p.parse_args()

    result = _collect(args)
    if args.json:
        print(json.dumps(build_summary(result, args.by), sort_keys=True))
        return

    _print_table(result)
    _report_selection(result, args.by)


if __name__ == "__main__":
    main()
