"""Unit tests for demand classification, per-segment method selection, and the backtest.

No DB: every test runs on hand-built dense monthly series so the math is deterministic.
"""

from __future__ import annotations

from app.services.forecasting import backtest, classify, selection
from scripts import run_backtest

# --- classification (Syntetos-Boylan quadrant) -------------------------------------------


def test_classify_smooth_regular_stable():
    # every month a sale, near-constant size => low ADI, low CV2 => smooth
    assert classify.classify([100.0, 105.0, 95.0, 100.0, 102.0, 98.0]) == classify.SMOOTH


def test_classify_intermittent_sporadic_stable_size():
    # sparse cadence (every 3rd month) but equal size => high ADI, low CV2 => intermittent
    assert classify.classify([100.0, 0.0, 0.0, 100.0, 0.0, 0.0, 100.0, 0.0, 0.0]) == (
        classify.INTERMITTENT
    )


def test_classify_lumpy_sporadic_and_volatile():
    # sparse AND wildly varying size => high ADI, high CV2 => lumpy
    series = [0.0, 0.0, 10.0, 0.0, 0.0, 0.0, 900.0, 0.0, 0.0, 5.0, 0.0, 0.0]
    assert classify.classify(series) == classify.LUMPY


def test_classify_no_demand_is_labelled_distinctly():
    assert classify.classify([0.0, 0.0, 0.0]) == classify.NO_DEMAND
    assert classify.classify([]) == classify.NO_DEMAND


def test_classify_trims_leading_prehistory_zeros():
    # 6 leading zeros (relationship didn't exist yet) then 6 dense stable months. Without the
    # trim, ADI = 12/6 = 2.0 > cutoff -> falsely intermittent; with the trim it is smooth.
    series = [0.0] * 6 + [100.0, 102.0, 98.0, 101.0, 99.0, 100.0]
    assert classify.adi(series) == 1.0
    assert classify.classify(series) == classify.SMOOTH


def test_classify_keeps_interior_droughts():
    # an interior zero IS a real drought and must still raise ADI above 1.
    series = [100.0, 0.0, 100.0, 0.0, 100.0, 0.0, 100.0]
    assert classify.adi(series) > 1.0


# --- method selection ---------------------------------------------------------------------


def test_default_segment_map_is_backtest_proven_choice():
    # Re-derived on the corrected full-sales data: smooth adopts EWMA (lowest MAE + near-zero
    # bias); erratic reverts to moving_avg (lowest MAE in every split on the corrected data);
    # intermittent/lumpy keep SBA; no_demand stays the safe moving_avg.
    assert selection.method_for_segment(classify.SMOOTH) == "ewma"
    assert selection.method_for_segment(classify.ERRATIC) == "moving_avg"
    assert selection.method_for_segment(classify.INTERMITTENT) == "sba"
    assert selection.method_for_segment(classify.LUMPY) == "sba"
    assert selection.method_for_segment(classify.NO_DEMAND) == "moving_avg"


def test_method_for_segment_defaults_safely_on_unknown():
    assert selection.method_for_segment("bogus_segment") == "moving_avg"
    # a mapped-but-invalid method also falls back to the safe default
    assert selection.method_for_segment("smooth", {"smooth": "not_a_method"}) == "moving_avg"


def test_select_method_auto_uses_segment():
    smooth = [100.0, 100.0, 100.0, 100.0]
    method, segment = selection.select_method(smooth, "auto")
    assert segment == classify.SMOOTH
    assert method == "ewma"  # adopted for smooth

    intermittent = [100.0, 0.0, 0.0, 100.0, 0.0, 0.0, 100.0]
    method, segment = selection.select_method(intermittent, "auto")
    assert segment == classify.INTERMITTENT
    assert method == "sba"


def test_select_method_routes_erratic_to_moving_avg():
    # erratic = regular cadence, volatile size (low ADI, high CV2) -> moving_avg under auto
    # (re-derived: window mean has the lowest MAE on the corrected full data for this segment).
    erratic = [100.0, 800.0, 50.0, 900.0, 120.0, 700.0]
    method, segment = selection.select_method(erratic, "auto")
    assert segment == classify.ERRATIC
    assert method == "moving_avg"


def test_select_method_forced_new_methods_are_runnable():
    series = [10.0, 20.0, 30.0, 40.0]
    for forced in ("ewma", "damped_trend"):
        method, _ = selection.select_method(series, forced)
        assert method == forced  # new methods are valid forced modes, not coerced to default


def test_select_method_forced_overrides_segment_but_reports_it():
    intermittent = [100.0, 0.0, 0.0, 100.0, 0.0, 0.0, 100.0]
    method, segment = selection.select_method(intermittent, "moving_avg")
    assert method == "moving_avg"  # forced
    assert segment == classify.INTERMITTENT  # still reported for logging


def test_select_method_empirical_override_map_wins_over_prior():
    intermittent = [100.0, 0.0, 0.0, 100.0, 0.0, 0.0, 100.0]
    override = {classify.INTERMITTENT: "croston"}
    method, _ = selection.select_method(intermittent, "auto", override)
    assert method == "croston"


# --- backtest (rolling-origin accuracy) ---------------------------------------------------


def test_backtest_perfect_constant_series_zero_error_for_unbiased_methods():
    # A perfectly constant series: moving_avg and croston forecast the constant exactly, so
    # their MAE/bias = 0. SBA deliberately applies the (1 - alpha/2) bias correction, so it
    # under-forecasts by exactly that factor even here — that is correct SBA behaviour and the
    # backtest must surface it as a small negative bias (the price SBA pays for de-biasing
    # Croston on intermittent series).
    series = [50.0] * 12
    result = backtest.BacktestResult()
    scored = backtest.backtest_series(
        series, result, horizon=2, eval_origins=6, min_train=3, alpha=0.1
    )
    assert scored
    for method in ("moving_avg", "croston"):
        acc = result.overall(method)
        assert acc.n > 0
        assert acc.mae == 0.0
        assert acc.bias == 0.0
        assert acc.smape == 0.0
    sba = result.overall("sba")
    assert sba.bias < 0  # systematically below the constant by the SBA correction
    assert abs(sba.bias) == 50.0 * (0.1 / 2.0)  # exactly 2.5


def test_backtest_no_leakage_uses_only_past():
    # If a future spike sits AFTER every evaluated origin, the forecast (trained on the flat
    # past only) cannot see it -> it under-forecasts -> negative bias. This proves the rolling
    # origin never leaks future data into the training window.
    series = [10.0] * 10 + [1000.0]
    result = backtest.BacktestResult()
    backtest.backtest_series(series, result, horizon=1, eval_origins=1, min_train=3, alpha=0.1)
    acc = result.overall("moving_avg")
    assert acc.n == 1
    assert acc.bias < 0  # forecast (~10) << actual (1000)


def test_backtest_thin_series_scores_nothing():
    # fewer usable months than min_train+1 -> no valid origin -> nothing scored, no crash.
    result = backtest.BacktestResult()
    assert not backtest.backtest_series(
        [10.0, 20.0], result, horizon=3, eval_origins=12, min_train=3, alpha=0.1
    )
    assert result.n_origins == 0


def test_backtest_smape_is_bounded_and_finite_on_zero_actual():
    # plain MAPE would be infinite when actual = 0; sMAPE stays finite (<= 2.0). A flat-positive
    # forecast against a zero actual is the worst case and must cap, not explode.
    acc = backtest.Accumulator()
    acc.add(forecast=100.0, actual=0.0)
    assert acc.smape == 2.0  # |100-0| / ((100+0)/2) = 2.0
    acc.add(forecast=0.0, actual=0.0)
    assert 0.0 <= acc.smape <= 2.0


def test_best_method_per_segment_picks_lowest_error():
    result = backtest.BacktestResult()
    # craft two methods on one segment: moving_avg worse, sba better.
    result.cell("moving_avg", classify.LUMPY).add(100.0, 0.0)  # abs err 100
    result.cell("sba", classify.LUMPY).add(10.0, 0.0)  # abs err 10
    chosen = backtest.best_method_per_segment(result, by="mae")
    assert chosen[classify.LUMPY] == "sba"


def test_run_backtest_json_summary_contract():
    result = backtest.BacktestResult()
    result.n_series = 2
    result.n_origins = 3
    result.segment_counts[classify.SMOOTH] = 1
    result.segment_counts[classify.LUMPY] = 1
    result.cell("moving_avg", classify.SMOOTH).add(20.0, 10.0)
    result.overall("moving_avg").add(20.0, 10.0)
    result.cell("ewma", classify.SMOOTH).add(11.0, 10.0)
    result.overall("ewma").add(11.0, 10.0)
    result.cell("moving_avg", classify.LUMPY).add(100.0, 0.0)
    result.overall("moving_avg").add(100.0, 0.0)
    result.cell("sba", classify.LUMPY).add(10.0, 0.0)
    result.overall("sba").add(10.0, 0.0)

    summary = run_backtest.build_summary(result, by="mae")

    assert summary["schema_version"] == 1
    assert summary["n_series"] == 2
    assert summary["by_method"]["moving_avg"]["n"] == 2
    assert summary["selection"]["best_method_per_segment"] == {
        classify.SMOOTH: "ewma",
        classify.LUMPY: "sba",
    }
    assert summary["selection"]["auto_mae"] == 5.5
    assert summary["selection"]["legacy_mae"] == 55.0
