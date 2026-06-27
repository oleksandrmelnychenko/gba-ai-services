"""Pure unit tests for month-name mapping + forecast-from-series shaping (no DB)."""

from __future__ import annotations

from datetime import date

from app.core.config import get_settings
from app.services import forecast as fc
from app.services.forecasting import demand

CFG = get_settings()


def test_month_name_uk_short_month_plus_year():
    assert fc.month_name_uk(2026, 7) == "Лип 2026"
    assert fc.month_name_uk(2026, 1) == "Січ 2026"
    assert fc.month_name_uk(2025, 12) == "Гру 2025"


def test_month_name_uk_full_mapping():
    expected = ["Січ", "Лют", "Бер", "Кві", "Тра", "Чер", "Лип", "Сер", "Вер", "Жов", "Лис", "Гру"]
    assert [fc.month_name_uk(2026, m).split()[0] for m in range(1, 13)] == expected


def test_history_labels_trailing_oldest_first():
    assert fc.history_labels(date(2026, 6, 24), 3) == ["2026-04", "2026-05", "2026-06"]
    # crosses a year boundary
    assert fc.history_labels(date(2026, 2, 1), 3) == ["2025-12", "2026-01", "2026-02"]


def test_series_from_fills_zeros():
    labels = fc.history_labels(date(2026, 6, 24), 3)
    assert fc.series_from({"2026-06": 100.0}, labels) == [0.0, 0.0, 100.0]


def test_future_months_after_as_of_with_year_rollover():
    assert fc.future_months(date(2026, 11, 10), 3) == [(2026, 12), (2027, 1), (2027, 2)]


def test_moving_avg_rate_is_window_mean():
    assert demand.moving_avg_rate([100.0, 200.0, 300.0]) == 200.0
    assert demand.moving_avg_rate([]) == 0.0


def test_forecast_series_flat_projection_of_rate():
    out = demand.forecast_series([100.0, 100.0, 100.0], 6, "moving_avg", 0.1)
    assert out == [100.0] * 6
    assert demand.forecast_series([0.0, 0.0], 6, "moving_avg", 0.1) == [0.0] * 6


def test_croston_rate_intermittent_size_over_interval():
    # Croston smooths demand SIZE (~100) and the inter-demand INTERVAL separately, then forecasts
    # rate = size / interval. For 100 every other month the rate sits between the per-event size
    # (100) and the dense window mean (50), reflecting the smoothed cadence.
    series = [100.0, 0.0, 100.0, 0.0, 100.0, 0.0]
    rate = demand.croston_rate(series, alpha=0.1, sba=False)
    assert 0 < rate <= 100.0
    # SBA applies the (1 - alpha/2) bias correction => strictly lower than plain Croston.
    assert demand.croston_rate(series, 0.1, sba=True) < rate


def test_ewma_rate_weights_recent_months_more_than_window_mean():
    # On an UP-trending series the EWMA level sits ABOVE the flat window mean (it tracks the
    # recent, higher months) — exactly why it cures the under-forecast bias the backtest found.
    rising = [10.0, 20.0, 30.0, 40.0, 50.0]
    mean = demand.moving_avg_rate(rising)  # 30.0
    ewma = demand.ewma_rate(rising, demand.EWMA_ALPHA)
    assert ewma > mean
    # On a falling series it sits below the mean; on a flat series it equals the level.
    assert demand.ewma_rate([50.0, 40.0, 30.0, 20.0, 10.0], demand.EWMA_ALPHA) < mean
    assert demand.ewma_rate([7.0, 7.0, 7.0, 7.0], demand.EWMA_ALPHA) == 7.0
    assert demand.ewma_rate([], demand.EWMA_ALPHA) == 0.0
    assert demand.ewma_rate([5.0], demand.EWMA_ALPHA) == 5.0  # single point => itself


def test_ewma_rate_never_negative():
    assert demand.ewma_rate([0.0, 0.0, 0.0], demand.EWMA_ALPHA) == 0.0


def test_damped_trend_path_extrapolates_recent_slope():
    # A steady +10/month uptrend: damped trend projects ABOVE the last level and INCREASES across
    # the horizon (level + (phi+phi^2+...)*slope), i.e. it tracks the trend instead of going flat.
    rising = [10.0, 20.0, 30.0, 40.0, 50.0]
    path = demand.damped_trend_path(rising, 3)
    assert len(path) == 3
    assert path[0] > 50.0  # above the most recent observed month
    assert path[0] < path[1] < path[2]  # keeps climbing with the (damped) trend
    # Damping bounds the run-away: each successive INCREMENT shrinks by phi (sub-linear growth).
    inc1, inc2 = path[1] - path[0], path[2] - path[1]
    assert inc2 < inc1


def test_damped_trend_path_is_clamped_and_degenerate_safe():
    # A sharp downtrend can drive the projection to zero but never below it.
    assert all(v >= 0.0 for v in demand.damped_trend_path([100.0, 50.0, 10.0, 0.0], 6))
    # Degenerate inputs: empty -> zeros, single point -> that level repeated.
    assert demand.damped_trend_path([], 3) == [0.0, 0.0, 0.0]
    assert demand.damped_trend_path([42.0], 2) == [42.0, 42.0]
    assert demand.damped_trend_path([10.0, 20.0], 0) == []


def test_forecast_path_flat_methods_repeat_rate_path_method_varies():
    rising = [10.0, 20.0, 30.0, 40.0]
    flat = demand.forecast_path(rising, 3, "ewma", 0.1)
    assert flat == [flat[0]] * 3  # ewma is a flat level repeated
    path = demand.forecast_path(rising, 3, "damped_trend", 0.1)
    assert path[0] != path[2]  # damped_trend varies per step
    # moving_avg stays the exact legacy flat projection.
    assert demand.forecast_path([100.0, 100.0], 4, "moving_avg", 0.1) == [100.0] * 4


def test_monthly_rate_reports_new_method_ids():
    _, mid = demand.monthly_rate([10.0, 20.0, 30.0], "ewma", 0.1)
    assert mid == demand._METHOD_EWMA
    _, mid = demand.monthly_rate([10.0, 20.0, 30.0], "damped_trend", 0.1)
    assert mid == demand._METHOD_DAMPED_TREND


def test_forecast_points_shapes_contract():
    hist = {"2026-04": 90.0, "2026-05": 120.0, "2026-06": 150.0}
    pts = fc.forecast_points(hist, date(2026, 6, 24), CFG, horizon=3)
    assert len(pts) == 3
    assert pts[0]["MonthNameUK"] == "Лип 2026"
    assert pts[1]["MonthNameUK"] == "Сер 2026"
    assert pts[2]["MonthNameUK"] == "Вер 2026"
    assert all(isinstance(p["SaleAmount"], float) and p["SaleAmount"] >= 0 for p in pts)


def test_forecast_points_empty_when_too_thin():
    thin_cfg = CFG.model_copy(update={"min_history_months": 2})
    # only one non-zero month, threshold is 2 => [] (console «немає даних»)
    assert fc.forecast_points({"2026-06": 10.0}, date(2026, 6, 24), thin_cfg, horizon=6) == []
    # genuinely empty history => []
    assert fc.forecast_points({}, date(2026, 6, 24), CFG, horizon=6) == []


def test_forecast_points_never_crashes_on_zero_horizon():
    assert fc.forecast_points({"2026-06": 10.0}, date(2026, 6, 24), CFG, horizon=0) == []


def test_forecast_points_auto_picks_sba_for_intermittent_series():
    # An intermittent series under auto mode is forecast with SBA (bias-corrected), which yields
    # a strictly lower flat value than the window-mean (moving_avg) on a sporadic series.
    hist = {"2026-01": 100.0, "2026-04": 100.0, "2026-06": 100.0}
    auto_cfg = CFG.model_copy(update={"forecast_method": "auto", "min_history_months": 3})
    fixed_cfg = CFG.model_copy(update={"forecast_method": "moving_avg", "min_history_months": 3})
    auto_pts = fc.forecast_points(hist, date(2026, 6, 24), auto_cfg, horizon=3)
    fixed_pts = fc.forecast_points(hist, date(2026, 6, 24), fixed_cfg, horizon=3)
    assert auto_pts and fixed_pts
    assert auto_pts[0]["SaleAmount"] < fixed_pts[0]["SaleAmount"]


def test_forecast_points_auto_matches_ewma_for_smooth_series():
    # A smooth (dense, stable) series under auto mode now resolves to EWMA, so the forecast is
    # byte-identical to the forced-ewma path (the adopted choice for smooth). Build a
    # constant series spanning the FULL history window so the EWMA level == the window mean.
    labels = fc.history_labels(date(2026, 6, 24), CFG.history_months)
    hist = dict.fromkeys(labels, 100.0)
    auto_cfg = CFG.model_copy(update={"forecast_method": "auto", "min_history_months": 3})
    ewma_cfg = CFG.model_copy(update={"forecast_method": "ewma", "min_history_months": 3})
    assert fc.forecast_points(hist, date(2026, 6, 24), auto_cfg, horizon=3) == fc.forecast_points(
        hist, date(2026, 6, 24), ewma_cfg, horizon=3
    )
    # On a flat, dense series EWMA == the window mean, so the value is still byte-identical to
    # legacy moving_avg — no regression for the common stable case.
    mavg_cfg = CFG.model_copy(update={"forecast_method": "moving_avg", "min_history_months": 3})
    assert fc.forecast_points(hist, date(2026, 6, 24), auto_cfg, horizon=3) == fc.forecast_points(
        hist, date(2026, 6, 24), mavg_cfg, horizon=3
    )


def test_forecast_points_auto_tracks_uptrend_above_window_mean():
    # The core fix: on an UP-trending smooth series, auto (EWMA) forecasts ABOVE the flat window
    # mean — it tracks the uptrend instead of lagging it (the negative-bias cure), and stays
    # within the response contract (PascalCase float SaleAmount).
    hist = {f"2026-{m:02d}": float(m * 100) for m in range(1, 7)}  # 100,200,...,600 (rising)
    auto_cfg = CFG.model_copy(update={"forecast_method": "auto", "min_history_months": 3})
    mavg_cfg = CFG.model_copy(update={"forecast_method": "moving_avg", "min_history_months": 3})
    auto_pts = fc.forecast_points(hist, date(2026, 6, 24), auto_cfg, horizon=3)
    mavg_pts = fc.forecast_points(hist, date(2026, 6, 24), mavg_cfg, horizon=3)
    assert auto_pts and mavg_pts
    assert auto_pts[0]["SaleAmount"] > mavg_pts[0]["SaleAmount"]  # tracks the trend, not lags it
