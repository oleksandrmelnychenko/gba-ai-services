"""Pure unit tests for month-name mapping + forecast-from-series shaping (no DB)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

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


def test_history_labels_and_dense_series_never_invent_pre_source_months():
    labels = fc.history_labels(
        date(2026, 7, 25),
        24,
        date(2025, 1, 1),
    )
    series = fc.series_from(
        {
            "2024-12": 999.0,
            "2025-01": 100.0,
            "2026-07": 200.0,
        },
        labels,
    )

    assert labels[0] == "2025-01"
    assert labels[-1] == "2026-07"
    assert len(labels) == len(series) == 19
    assert not any(label.startswith("2024-") for label in labels)
    assert series[0] == 100.0
    assert 999.0 not in series


def test_history_labels_reject_as_of_before_source_floor():
    with pytest.raises(ValueError, match="as_of_date_before_source_history_start"):
        fc.history_labels(date(2024, 12, 31), 24, date(2025, 1, 1))


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
    hist = {"2026-03": 90.0, "2026-04": 120.0, "2026-05": 150.0, "2026-06": 20.0}
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


@pytest.mark.parametrize("day,partial", [(1, 0.0), (7, 60.0), (30, 9000.0)])
def test_partial_month_does_not_change_forecast_of_stable_closed_months(day, partial):
    history = {f"2025-{month:02d}": 300.0 for month in range(1, 13)}
    history.update({f"2026-{month:02d}": 300.0 for month in range(1, 9)})
    history["2026-09"] = partial
    config = CFG.model_copy(update={"forecast_method": "auto"})
    points = fc.forecast_points(history, date(2026, 9, day), config, horizon=3)
    assert [point["SaleAmount"] for point in points] == [300.0, 300.0, 300.0]
    assert points[0]["MonthNameUK"] == "Жов 2026"


def test_trend_projection_skips_current_month_but_keeps_next_month_labels():
    history = {"2026-03": 100.0, "2026-04": 200.0, "2026-05": 300.0, "2026-06": 1.0}
    config = CFG.model_copy(update={"forecast_method": "damped_trend", "history_months": 4})
    points = fc.forecast_points(history, date(2026, 6, 7), config, horizon=2)
    expected = demand.damped_trend_path([100.0, 200.0, 300.0], 3)
    assert [point["SaleAmount"] for point in points] == [fc.eur_cents(x) for x in expected[1:]]
    assert [point["MonthNameUK"] for point in points] == ["Лип 2026", "Сер 2026"]


def test_eur_boundary_uses_decimal_half_up_and_rejects_non_finite():
    assert fc.eur_cents(Decimal("10.004")) == 10.0
    assert fc.eur_cents(Decimal("10.005")) == 10.01
    assert fc.eur_cents(Decimal("10.995")) == 11.0
    assert fc.eur_cents(Decimal("-0.01")) == 0.0

    for invalid in (float("nan"), float("inf"), float("-inf"), "not-a-number"):
        with pytest.raises(ValueError, match="finite"):
            fc.eur_cents(invalid)


def test_forecast_points_auto_uses_tsb_for_intermittent_client_series():
    hist = {"2025-12": 100.0, "2026-01": 100.0, "2026-04": 100.0}
    auto_cfg = CFG.model_copy(update={"forecast_method": "auto", "min_history_months": 3})
    sba_cfg = CFG.model_copy(update={"forecast_method": "sba", "min_history_months": 3})
    as_of = date(2026, 6, 24)
    points, method = fc.forecast_points_with_method(hist, as_of, auto_cfg, horizon=3)
    series = fc.series_from(hist, fc.training_labels(as_of, auto_cfg))
    assert method == {"method": "tsb_a0.5_b0.5", "segment": "intermittent"}
    assert points[0]["SaleAmount"] == fc.eur_cents(demand.tsb_rate(series, 0.5, 0.5))
    assert len({point["SaleAmount"] for point in points}) == 1
    pair_points, pair_method = fc.forecast_points_with_method(hist, as_of, auto_cfg, 3, "pair")
    assert pair_method == {"method": "ewma_a0.4", "segment": "intermittent"}
    assert pair_points[0]["SaleAmount"] == fc.eur_cents(demand.ewma_rate(series, 0.4))
    assert points == fc.forecast_points(hist, as_of, auto_cfg, horizon=3)
    assert points != fc.forecast_points(hist, as_of, sba_cfg, horizon=3)
    keys = {"SaleAmount", "MonthNameUK", "SaleAmountLow", "SaleAmountHigh"}
    assert [set(point) for point in points] == [keys] * 3


def test_forecast_points_with_method_reports_nothing_for_refused_series():
    hist = {"2026-01": 100.0, "2026-04": 100.0}
    cfg = CFG.model_copy(update={"forecast_method": "auto", "min_history_months": 3})
    assert fc.forecast_points_with_method(hist, date(2026, 6, 24), cfg, horizon=3) == ([], None)


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


def test_partial_month_cannot_make_history_status_sufficient(monkeypatch):
    from app.api import main
    from app.core.config import Settings
    settings = Settings(min_history_months=3, history_months=12)
    monkeypatch.setattr(main, "settings", settings)
    rows = [{"ym": month, "eur": 300.0} for month in ("2026-07", "2026-08", "2026-09")]
    points, history, method = main._forecast_with_history(rows, date(2026, 9, 7), 3)
    assert points == []
    assert method is None
    assert history["sufficient"] is False
    assert history["status"] == "insufficient_history"
    assert history["month_count"] == 2


def test_forecast_points_use_the_scope_alpha():
    values = [100, 120, 90, 110, 130, 95, 105, 115, 125, 100, 300, 320]
    hist = {f"2025-{month:02d}": float(value) for month, value in enumerate(values, start=1)}
    cfg = CFG.model_copy(update={"forecast_method": "auto"})
    as_of = date(2026, 1, 15)
    series = fc.series_from(hist, fc.training_labels(as_of, cfg))
    for scope, alpha in (("client", 0.3), ("product", 0.15), ("pair", 0.1)):
        points, method = fc.forecast_points_with_method(hist, as_of, cfg, 2, scope)
        assert method == {"method": f"ewma_a{alpha:g}", "segment": "smooth"}
        assert points[0]["SaleAmount"] == fc.eur_cents(demand.ewma_rate(series, alpha))


def _history_then_silence(active: int, silent: int, amount: float = 1000.0) -> tuple[dict, date]:
    labels = fc.history_labels(date(2026, 11, 13), active + silent + 1, date(2025, 1, 1))[:-1]
    return {label: amount for label in labels[:active]}, date(2026, 11, 13)


@pytest.mark.parametrize(
    ("scope", "silent", "ceiling"),
    [
        ("client", 6, 20.0),
        ("client", 12, 0.5),
        ("product", 6, 130.0),
        ("product", 12, 20.0),
        ("pair", 12, 5.0),
    ],
)
def test_a_series_that_stopped_selling_fades_to_zero(scope, silent, ceiling):
    hist, as_of = _history_then_silence(22 - silent, silent)
    cfg = CFG.model_copy(update={"forecast_method": "auto", "history_months": 24})
    points, method = fc.forecast_points_with_method(hist, as_of, cfg, 6, scope)
    assert method["segment"] == "intermittent"
    assert len(points) == 6
    assert all(0.0 <= point["SaleAmount"] <= ceiling for point in points)
    assert all(point["SaleAmountLow"] <= point["SaleAmount"] <= point["SaleAmountHigh"] for point in points)


def test_recent_silence_lowers_the_forecast_month_by_month():
    cfg = CFG.model_copy(update={"forecast_method": "auto", "history_months": 24})
    amounts = []
    for silent in range(0, 8):
        hist, as_of = _history_then_silence(22 - silent, silent)
        amounts.append(fc.forecast_points(hist, as_of, cfg, 1, "client")[0]["SaleAmount"])
    assert amounts[0] == 1000.0
    assert all(later < earlier for earlier, later in zip(amounts, amounts[1:], strict=False))


def test_new_entity_with_two_months_gets_no_forecast_and_three_gets_one():
    cfg = CFG.model_copy(update={"forecast_method": "auto", "history_months": 24})
    as_of = date(2026, 11, 13)
    two = {"2026-09": 500.0, "2026-10": 700.0, "2026-11": 900.0}
    assert fc.forecast_points_with_method(two, as_of, cfg, 6, "client") == ([], None)
    three = {"2026-08": 400.0, **two}
    points, method = fc.forecast_points_with_method(three, as_of, cfg, 6, "client")
    assert len(points) == 6 and method["segment"] == "smooth"
    series = fc.series_from(three, fc.training_labels(as_of, cfg))
    assert series[:19] == [0.0] * 19
    assert points[0]["SaleAmount"] == fc.eur_cents(demand.ewma_rate(series, 0.3)) == 373.8
    assert points[0]["SaleAmount"] < demand.ewma_rate(series[19:], 0.3)


def test_tsb_rate_is_never_negative_and_handles_degenerate_input():
    assert demand.tsb_rate([], 0.5, 0.5) == 0.0
    assert demand.tsb_rate([0.0, 0.0, 0.0], 0.5, 0.5) == 0.0
    assert demand.tsb_rate([0.0, 0.0, 300.0], 0.5, 0.5) == 150.0
    assert demand.tsb_rate([200.0] * 6, 0.2, 0.3) == pytest.approx(200.0)
    assert demand.tsb_rate([200.0, 0.0, 0.0, 0.0], 0.5, 0.5) == pytest.approx(25.0)
