from datetime import date, timedelta

import pytest

from app.services.forecasting import demand


def test_missing_month_rows_are_equivalent_to_explicit_zero_months():
    sparse = [{"d": "2026-06-15", "units": 300}]
    dense = [*sparse, {"d": "2026-07-15", "units": 0}, {"d": "2026-08-15", "units": 0}]
    args = (.35, date(2026, 6, 1), date(2026, 9, 1))
    expected_rate = 10 * .65 * .65
    assert demand._ewma_monthly_rate(sparse, *args) == pytest.approx(expected_rate)
    assert demand._ewma_monthly_rate(dense, *args) == pytest.approx(expected_rate)


def test_constant_daily_demand_stays_constant_across_partial_months():
    start = date(2026, 6, 9)
    rows = [{"d": start + timedelta(days=day), "units": 10} for day in range(90)]
    result = demand.forecast_from_rows(
        1, rows, 30, method="ewma", effective_start=start, effective_history_days=90,
    )
    assert result.mean_daily == pytest.approx(10)
    assert result.forecast_units == pytest.approx(300)


def test_window_crosses_year_and_drops_rows_outside_exclusive_boundary():
    rows = [{"d": "2025-12-15", "units": 310}, {"d": "2026-02-01", "units": 100000}]
    result = demand._ewma_monthly_rate(rows, .5, date(2025, 12, 1), date(2026, 2, 1))
    assert result == pytest.approx(5)


def test_zero_days_cannot_generate_demand():
    assert demand._ewma_monthly_rate([{"d": "2026-06-15", "units": 300}], .35,
                                     date(2026, 6, 1), date(2026, 6, 1)) == 0
