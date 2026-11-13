from __future__ import annotations

from datetime import date

import pytest

from app.core.config import get_settings
from app.services import forecast as fc
from app.services.forecasting import classify, intervals, selection

CLASSES = (classify.SMOOTH, classify.ERRATIC, classify.INTERMITTENT, classify.LUMPY)


def test_table_covers_every_scope_class_band_and_step():
    table = intervals.table()
    assert set(table["scopes"]) == set(selection.SCOPES)
    steps = intervals.LAST_STEP - intervals.FIRST_STEP + 1
    for scope in selection.SCOPES:
        for segment in CLASSES:
            entry = table["scopes"][scope][segment]
            edges = entry["size_edges_eur"]
            assert edges == sorted(edges) and len(entry["bands"]) == len(edges) + 1
            for band in entry["bands"]:
                assert len(band) == steps
                for step, (low, high, count) in enumerate(band, start=intervals.FIRST_STEP):
                    assert 0.0 <= low <= high
                    assert count >= 0
                    served = intervals.multipliers(scope, segment, 1e9, step)
                    assert 0.0 <= served[0] <= 1.0 <= served[1]


def test_out_of_sample_coverage_is_close_to_nominal():
    for scope, block in intervals.table()["out_of_sample"].items():
        for segment, cell in block["by_class"].items():
            assert cell["points"] >= 1000, (scope, segment)
            assert 78.0 <= cell["inside_pct"] <= 95.0, (scope, segment)
            assert cell["above_pct"] <= 17.0, (scope, segment)


def test_larger_client_forecasts_get_a_tighter_range():
    edges = intervals.table()["scopes"]["client"][classify.SMOOTH]["size_edges_eur"]
    small = intervals.multipliers("client", classify.SMOOTH, edges[0] / 2, 2)
    large = intervals.multipliers("client", classify.SMOOTH, edges[-1] * 2, 2)
    assert large[1] - large[0] < small[1] - small[0]
    assert large[0] > 0.0


def test_steps_outside_the_table_reuse_the_nearest_one():
    args = ("product", classify.LUMPY, 100.0)
    assert intervals.multipliers(*args, 1) == intervals.multipliers(*args, intervals.FIRST_STEP)
    assert intervals.multipliers(*args, 24) == intervals.multipliers(*args, intervals.LAST_STEP)


def test_band_index_and_unknown_inputs():
    assert [intervals.band_index([10.0, 100.0], value) for value in (5.0, 10.0, 50.0, 500.0)] == [
        0,
        0,
        1,
        2,
    ]
    assert intervals.multipliers("client", classify.NO_DEMAND, 50.0, 2) == intervals.multipliers(
        "client", classify.LUMPY, 50.0, 2
    )
    with pytest.raises(ValueError, match="unknown forecast scope"):
        intervals.multipliers("warehouse", classify.SMOOTH, 50.0, 2)


def test_points_carry_a_range_around_the_forecast():
    cfg = get_settings().model_copy(update={"forecast_method": "auto"})
    history = {f"2025-{month:02d}": 1000.0 + 50.0 * (month % 3) for month in range(1, 13)}
    points, method = fc.forecast_points_with_method(history, date(2026, 1, 15), cfg, 8, "client")
    assert method["segment"] == classify.SMOOTH
    assert len(points) == 8
    for offset, point in enumerate(points):
        low, high = intervals.multipliers(
            "client", classify.SMOOTH, point["SaleAmount"], intervals.FIRST_STEP + offset
        )
        assert point["SaleAmountLow"] == fc.eur_cents(point["SaleAmount"] * low)
        assert point["SaleAmountHigh"] == fc.eur_cents(point["SaleAmount"] * high)
        assert point["SaleAmountLow"] <= point["SaleAmount"] <= point["SaleAmountHigh"]
    assert points[7]["SaleAmountHigh"] == points[5]["SaleAmountHigh"]


def test_metadata_names_the_table():
    meta = intervals.metadata()
    assert meta["coverage_pct"] == 80
    assert meta["table_version"] == intervals.table()["table_version"]


def test_upper_bound_widening_is_recorded_and_never_shrinks_with_the_step():
    payload = intervals.table()
    assert set(payload["upper_widening"]) == set(selection.SCOPES)
    for scope, by_step in payload["upper_widening"].items():
        factors = [by_step[str(step)] for step in range(intervals.FIRST_STEP, intervals.LAST_STEP + 1)]
        assert factors[0] >= 1.0, scope
        assert factors == sorted(factors), scope
    assert payload["upper_widening"]["client"]["7"] > 1.0


def test_out_of_sample_exceedance_per_step_is_at_most_nominal_after_widening():
    for scope, block in intervals.table()["out_of_sample"].items():
        for step, cell in block["by_step"].items():
            before = block["by_step_before_widening"][step]
            assert cell["above_pct"] <= 10.0, (scope, step)
            assert cell["above_pct"] <= before["above_pct"], (scope, step)
            assert cell["inside_pct"] >= before["inside_pct"], (scope, step)


def test_builder_widens_only_the_upper_bound_where_later_targets_exceed_it():
    from scripts import build_interval_table as builder

    table = {
        classify.SMOOTH: {
            "size_edges_eur": [10.0, 20.0],
            "bands": [[[0.5, 2.0, 300] for _ in builder.STEPS] for _ in range(builder.BANDS)],
        }
    }
    rows = []
    for step in builder.STEPS:
        exceed = 10 if step < 5 else 30
        rows += [(classify.SMOOTH, step, 20, 15.0, 2.2)] * exceed
        rows += [(classify.SMOOTH, step, 20, 15.0, 1.0)] * (100 - exceed)
    factors = builder.upper_widening(table, rows)
    assert [factors[step] for step in builder.STEPS] == [1.0, 1.0, 1.0, 1.2, 1.2, 1.2]
    wide = builder.widened(table, factors)
    cells = wide[classify.SMOOTH]["bands"][0]
    assert [cell[1] for cell in cells] == [2.0, 2.0, 2.0, 2.2, 2.2, 2.2]
    assert all(cell[0] == 0.5 and cell[2] == 300 for cell in cells)
    assert builder.coverage(table, rows, by_step=True)["5"] == {
        "points": 100,
        "inside_pct": 70.0,
        "above_pct": 30.0,
    }
    assert builder.coverage(wide, rows, by_step=True)["5"]["above_pct"] == 0.0
    assert builder.coverage(wide, rows)[classify.SMOOTH]["points"] == 600


def test_builder_records_use_the_service_method():
    from scripts import build_interval_table as builder

    sporadic = [0.0, 300.0, 0.0, 0.0, 200.0, 0.0, 400.0, 0.0, 0.0, 250.0, 0.0, 0.0]
    rows = builder.records({"7": sporadic}, selection.SCOPE_CLIENT, 3)
    assert rows
    segment, step, target, forecast, ratio = rows[0]
    train = sporadic[: target - step + 1]
    assert forecast == selection.auto_rate(train, segment, selection.SCOPE_CLIENT)[0]
    assert ratio == sporadic[target] / forecast
