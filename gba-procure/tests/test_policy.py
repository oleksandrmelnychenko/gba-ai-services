"""Fast unit tests for procurement math — no DB/Redis (pure logic)."""
from __future__ import annotations

import pytest

from app.data.db import in_clause
from app.domain.models import DemandForecast, InventoryPosition, Urgency
from app.services.replenishment import policy


def test_in_clause_parameterized():
    ph, params = in_clause("p", [1, 2])
    assert ph == "(:p0,:p1)"
    assert params == {"p0": 1, "p1": 2}


def test_z_for_known_service_levels():
    assert policy._z_for(0.95) == pytest.approx(1.6449, abs=1e-3)
    assert policy._z_for(0.99) == pytest.approx(2.3263, abs=1e-3)
    assert policy._z_for(0.975) == pytest.approx(1.9600, abs=1e-3)
    assert policy._z_for(0.50) == pytest.approx(0.0, abs=1e-6)
    assert policy._z_for(0.123) < 0


def test_learned_factors_median_clamp_and_min_samples(monkeypatch):
    from app.data import feedback

    docs = (
        [{"suggested_qty": 100, "final_qty": 120, "abc": "A"} for _ in range(5)]
        + [{"suggested_qty": 100, "final_qty": 50, "abc": "C"} for _ in range(5)]
        + [{"suggested_qty": 100, "final_qty": 9999, "abc": "B"} for _ in range(5)]
    )

    class _FakeColl:
        def find(self, *a, **k):
            return iter(docs)

    monkeypatch.setattr(feedback, "_feedback", lambda: _FakeColl())
    f = feedback.learned_factors(1, min_samples=5, lo=0.5, hi=1.5)
    assert f["A"] == 1.2
    assert f["C"] == 0.5
    assert f["B"] == 1.5
    assert feedback.learned_factors(1, min_samples=6, lo=0.5, hi=1.5) == {}


def test_round_order_qty_moq_and_multiple():
    assert policy._round_order_qty(0, 100, 50) == 0.0
    assert policy._round_order_qty(203.15, 100, 50) == 250.0
    assert policy._round_order_qty(40, 100, 50) == 100.0
    assert policy._round_order_qty(10, None, None) == 10.0
    assert policy._round_order_qty(10, 0, 1) == 10.0


def test_method_for_xyz_dispatch():
    from app.services.forecasting import demand as d
    assert d.method_for_xyz("Z") == "sba"
    assert d.method_for_xyz("Y") == "ewma"
    assert d.method_for_xyz("X") == "moving_avg"
    assert d.method_for_xyz(None) == "moving_avg"


def test_ewma_forecast_is_positive_and_tagged():
    from app.services.forecasting import demand as d
    rows = [{"d": f"2026-{m:02d}-15", "units": m * 10} for m in range(1, 7)]
    f = d.forecast_from_rows(1, rows, 30, method="ewma")
    assert f.method == "ewma_v1"
    assert f.mean_daily > 0


def test_abc_from_revenue_pareto():
    from app.services.classify import segmentation as seg
    assert seg.abc_from_revenue({1: 80.0, 2: 15.0, 3: 5.0}) == {1: "A", 2: "B", 3: "C"}
    assert seg.abc_from_revenue({1: 0.0, 2: 0.0}) == {1: "C", 2: "C"}


def test_abc_dominant_item_is_a():
    from app.services.classify import segmentation as seg
    assert seg.abc_from_revenue({1: 100.0}) == {1: "A"}
    assert seg.abc_from_revenue({1: 90.0, 2: 9.0, 3: 1.0})[1] == "A"


def test_xyz_regular_vs_intermittent_and_zero_months():
    from app.services.classify import segmentation as seg
    regular = [{"d": f"2026-{m:02d}-15", "units": 10} for m in range(1, 7)]
    assert seg.xyz_from_daily(regular, "2026-06-30", 180)[0] == "X"
    assert seg.xyz_from_daily([{"d": "2026-01-15", "units": 60}], "2026-06-30", 180)[0] == "Z"
    sparse = [{"d": "2025-08-15", "units": 30}, {"d": "2026-01-15", "units": 30}]
    assert seg.xyz_from_daily(sparse, "2026-06-30", 365)[0] == "Z"


def test_king_safety_stock_uses_lead_time_variance():
    fc = DemandForecast(product_id=1, mean_daily=10.0, std_daily=3.0, method="t",
                        horizon_days=30, forecast_units=300.0)
    inv = InventoryPosition(product_id=1, on_hand=0, reserved=0, on_order=0,
                            available=0, position=0)
    with_lt_var = policy._suggest_one(1, 9, fc, inv, lead_time_days=30,
                                      lead_time_std_days=15, z=2.3263, horizon=30)
    demand_only = policy._suggest_one(1, 9, fc, inv, lead_time_days=30,
                                      lead_time_std_days=0, z=2.3263, horizon=30)
    assert with_lt_var.safety_stock > demand_only.safety_stock
    assert with_lt_var.safety_stock == pytest.approx(351.03, abs=0.5)


def test_urgency_tracks_reorder_decision():
    fc = DemandForecast(product_id=1, mean_daily=2.0, std_daily=1.0, method="t",
                        horizon_days=30, forecast_units=60.0)
    inv = InventoryPosition(product_id=1, on_hand=0, reserved=0, on_order=0,
                            available=0, position=0)
    sug = policy._suggest_one(1, 9, fc, inv, lead_time_days=10, lead_time_std_days=2,
                              z=1.6449, horizon=30)
    assert sug.suggested_qty > 0
    assert sug.urgency != Urgency.NONE


def test_abc_floor_lifts_low_margin_important_items():
    from app.core.config import get_settings
    s = get_settings()
    sl_a = policy._economic_service_level(10.1, 10.0, 30, 30, s, abc="A")
    sl_c = policy._economic_service_level(10.1, 10.0, 30, 30, s, abc="C")
    assert sl_a == pytest.approx(s.service_level_floor_a, abs=1e-9)
    assert sl_c < sl_a


def test_economic_service_level_bounds_and_monotonicity():
    from app.core.config import get_settings
    s = get_settings()
    assert policy._economic_service_level(5.0, 6.0, 30, 30, s) == s.service_level_min
    sl_low = policy._economic_service_level(50.5, 50.0, 30, 30, s)
    sl_high = policy._economic_service_level(55.0, 50.0, 30, 30, s)
    assert s.service_level_min <= sl_low <= sl_high <= s.service_level_max
    assert sl_high > sl_low


def test_reorder_math_triggers_when_below_rop():
    fc = DemandForecast(product_id=1, mean_daily=2.0, std_daily=1.0, method="t",
                        horizon_days=30, forecast_units=60.0)
    inv = InventoryPosition(product_id=1, on_hand=5, reserved=0, on_order=0,
                            available=5, position=5)
    sug = policy._suggest_one(1, 99, fc, inv, lead_time_days=10, z=1.6449, horizon=30)
    # lead_demand=20, safety=1.6449*sqrt(10)*1 ~= 5.2, ROP ~= 25.2 ; position 5 << ROP -> order
    assert sug.suggested_qty > 0
    assert sug.reorder_point > 20
    assert sug.urgency in (Urgency.CRITICAL, Urgency.HIGH)


def test_no_order_when_well_stocked():
    fc = DemandForecast(product_id=1, mean_daily=1.0, std_daily=0.0, method="t",
                        horizon_days=30, forecast_units=30.0)
    inv = InventoryPosition(product_id=1, on_hand=1000, reserved=0, on_order=0,
                            available=1000, position=1000)
    sug = policy._suggest_one(1, 99, fc, inv, lead_time_days=10, z=1.6449, horizon=30)
    assert sug.suggested_qty == 0.0
    assert sug.urgency == Urgency.NONE


def test_zero_demand_is_infinite_cover():
    fc = DemandForecast(product_id=1, mean_daily=0.0, std_daily=0.0, method="t",
                        horizon_days=30, forecast_units=0.0)
    inv = InventoryPosition(product_id=1, on_hand=10, reserved=0, on_order=0,
                            available=10, position=10)
    sug = policy._suggest_one(1, 99, fc, inv, lead_time_days=10, z=1.6449, horizon=30)
    assert sug.suggested_qty == 0.0


def test_lead_time_geo_fallback_by_currency(monkeypatch):
    from app.services.forecasting import lead_time as lt
    monkeypatch.setattr(lt.repo, "producer_lead_times", lambda pid, asof, lo, hi: [])
    monkeypatch.setattr(lt.repo, "producer_agreement_currency", lambda pid: 10038)
    mean, std, src = lt.producer_lead_time(1, "2026-06-15")
    assert mean == 7.0 and src == "geo" and std > 0
    monkeypatch.setattr(lt.repo, "producer_agreement_currency", lambda pid: 3)
    assert lt.producer_lead_time(1, "2026-06-15")[0] == 35.0
    monkeypatch.setattr(lt.repo, "producer_agreement_currency", lambda pid: None)
    assert lt.producer_lead_time(1, "2026-06-15")[2] == "default"


def test_seasonal_index_shrinks_and_bounds():
    from app.services.forecasting import demand as d
    assert d.seasonal_index_for([{"d": "2026-01-15", "units": 10}], 1, min_months=12) == 1.0
    rows = [{"d": f"{y}-{m:02d}-15", "units": (100 if m == 6 else 10)}
            for y in (2024, 2025) for m in range(1, 13)]
    idx_jun = d.seasonal_index_for(rows, 6, k=4, lo=0.6, hi=1.6, min_months=12)
    idx_jan = d.seasonal_index_for(rows, 1, k=4, lo=0.6, hi=1.6, min_months=12)
    assert idx_jun > idx_jan
    assert 0.6 <= idx_jan <= idx_jun <= 1.6
