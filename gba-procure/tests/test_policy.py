"""Fast unit tests for procurement math — no DB/Redis (pure logic)."""

from __future__ import annotations

import pytest
from native_cost_fixture import make_cohort

from app.data import cost_repository as cost_repo
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


def test_money_rounding_is_decimal_half_up():
    assert policy._line_cost(1.005, 1) == 1.01
    assert policy._line_cost(2.675, 1) == 2.68
    assert policy._line_cost(0.335, 3) == 1.01


def test_buyer_unit_cost_override_wins_over_factual_cost():
    assert policy._resolved_unit_cost(10.0, {"unit_cost_override": 12.3456}) == 12.3456
    assert policy._resolved_unit_cost(10.0, {"unit_cost_override": 0}) == 10.0
    assert policy._resolved_unit_cost(10.0, None) == 10.0


def test_supplier_cost_median_preserves_verified_decimal_precision():
    cohort = make_cohort([1], as_of="2026-07-25", costs={(10, 1): ["1.0001", "1.0002"]})
    assert cost_repo.producer_unit_costs_eur(10, [1], "2026-07-25", cohort=cohort) == {1: 1.0002}


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
    assert f.method == "ewma_v2_calendar_daily"
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
    fc = DemandForecast(
        product_id=1, mean_daily=10.0, std_daily=3.0, method="t", horizon_days=30, forecast_units=300.0
    )
    inv = InventoryPosition(product_id=1, on_hand=0, reserved=0, on_order=0, available=0, position=0)
    with_lt_var = policy._suggest_one(
        1, 9, fc, inv, lead_time_days=30, lead_time_std_days=15, z=2.3263, horizon=30
    )
    demand_only = policy._suggest_one(
        1, 9, fc, inv, lead_time_days=30, lead_time_std_days=0, z=2.3263, horizon=30
    )
    assert with_lt_var.safety_stock > demand_only.safety_stock
    assert with_lt_var.safety_stock == pytest.approx(351.03, abs=0.5)


def test_urgency_tracks_reorder_decision():
    fc = DemandForecast(
        product_id=1, mean_daily=2.0, std_daily=1.0, method="t", horizon_days=30, forecast_units=60.0
    )
    inv = InventoryPosition(product_id=1, on_hand=0, reserved=0, on_order=0, available=0, position=0)
    sug = policy._suggest_one(1, 9, fc, inv, lead_time_days=10, lead_time_std_days=2, z=1.6449, horizon=30)
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
    fc = DemandForecast(
        product_id=1, mean_daily=2.0, std_daily=1.0, method="t", horizon_days=30, forecast_units=60.0
    )
    inv = InventoryPosition(product_id=1, on_hand=5, reserved=0, on_order=0, available=5, position=5)
    sug = policy._suggest_one(1, 99, fc, inv, lead_time_days=10, z=1.6449, horizon=30)
    # lead_demand=20, safety=1.6449*sqrt(10)*1 ~= 5.2, ROP ~= 25.2 ; position 5 << ROP -> order
    assert sug.suggested_qty > 0
    assert sug.reorder_point > 20
    assert sug.urgency in (Urgency.CRITICAL, Urgency.HIGH)


def test_no_order_when_well_stocked():
    fc = DemandForecast(
        product_id=1, mean_daily=1.0, std_daily=0.0, method="t", horizon_days=30, forecast_units=30.0
    )
    inv = InventoryPosition(product_id=1, on_hand=1000, reserved=0, on_order=0, available=1000, position=1000)
    sug = policy._suggest_one(1, 99, fc, inv, lead_time_days=10, z=1.6449, horizon=30)
    assert sug.suggested_qty == 0.0
    assert sug.urgency == Urgency.NONE


def test_zero_demand_is_infinite_cover():
    fc = DemandForecast(
        product_id=1, mean_daily=0.0, std_daily=0.0, method="t", horizon_days=30, forecast_units=0.0
    )
    inv = InventoryPosition(product_id=1, on_hand=10, reserved=0, on_order=0, available=10, position=10)
    sug = policy._suggest_one(1, 99, fc, inv, lead_time_days=10, z=1.6449, horizon=30)
    assert sug.suggested_qty == 0.0


def test_lead_time_geo_fallback_by_currency(monkeypatch):
    from app.services.forecasting import lead_time as lt

    monkeypatch.setattr(lt.repo, "producer_lead_times", lambda pid, asof, lo, hi: [])
    monkeypatch.setattr(lt.repo, "producer_agreement_currency", lambda pid, asof: 10038)
    mean, std, src = lt.producer_lead_time(1, "2026-06-15")
    assert mean == 7.0 and src == "geo" and std > 0
    monkeypatch.setattr(lt.repo, "producer_agreement_currency", lambda pid, asof: 3)
    assert lt.producer_lead_time(1, "2026-06-15")[0] == 35.0
    monkeypatch.setattr(lt.repo, "producer_agreement_currency", lambda pid, asof: None)
    assert lt.producer_lead_time(1, "2026-06-15")[2] == "default"


def test_empirical_lead_time_adds_the_order_processing_leg(monkeypatch):
    from app.services.forecasting import lead_time as lt

    samples = [10.0, 12.0, 14.0, 16.0, 18.0]
    monkeypatch.setattr(lt.repo, "producer_lead_times", lambda pid, asof, lo, hi: samples)
    mean, std, src = lt.producer_lead_time(1, "2026-06-15")
    allowance = lt.get_settings().lead_time_order_processing_days
    assert src == "empirical"
    assert mean == 14.0 + allowance
    assert std == max(8.0**0.5, mean * 0.1)


def test_fewer_than_five_receipt_intervals_keep_the_currency_default():
    from app.services.forecasting import lead_time as lt

    assert lt.get_settings().lead_time_min_samples == 5
    assert lt.lead_time_from([10.0, 10.0, 10.0, 10.0], 2) == (18.0, 9.0, "geo")
    assert lt.lead_time_from([10.0, 10.0, 10.0, 10.0], None)[2] == "default"
    assert lt.lead_time_from([10.0] * 5, 2)[2] == "empirical"


def test_noisy_measurement_uses_the_trimmed_mean_and_a_capped_spread():
    from app.services.forecasting import lead_time as lt

    samples = [6.0, 8.0, 9.0, 10.0, 11.0, 12.0, 14.0, 120.0]
    plain = sum(samples) / len(samples)
    mean, std, src = lt.lead_time_from(samples, 3)
    allowance = lt.get_settings().lead_time_order_processing_days
    assert src == "empirical_trimmed"
    assert mean == pytest.approx(sum(samples[1:-1]) / 6 + allowance)
    assert mean < plain
    assert std == pytest.approx(mean * lt.get_settings().lead_time_cv)
    assert lt.basis_of(src) == "measured"
    assert lt.payload_source(src) == "empirical"
    assert lt.statistic_of(src) == "trimmed_mean"


def test_noisy_spread_cap_can_be_switched_off(monkeypatch):
    from app.services.forecasting import lead_time as lt

    samples = [6.0, 8.0, 9.0, 10.0, 11.0, 12.0, 14.0, 120.0]
    settings = lt.get_settings().model_copy(update={"lead_time_cap_noisy_spread": False})
    monkeypatch.setattr(lt, "get_settings", lambda: settings)
    mean, std, src = lt.lead_time_from(samples, 3)
    plain = sum(samples) / len(samples)
    assert src == "empirical_trimmed"
    assert std == pytest.approx((sum((x - plain) ** 2 for x in samples) / len(samples)) ** 0.5)
    assert std > mean * settings.lead_time_cv


def test_steady_measurement_keeps_the_plain_mean():
    from app.services.forecasting import lead_time as lt

    samples = [10.0, 11.0, 12.0, 13.0, 14.0, 30.0]
    mean, std, src = lt.lead_time_from(samples, 3)
    assert src == "empirical"
    assert mean == pytest.approx(15.0 + lt.get_settings().lead_time_order_processing_days)
    assert lt.statistic_of(src) == "mean"
    assert lt.statistic_of("geo") is None


def test_trimmed_mean_drops_a_fifth_from_each_end():
    from app.services.forecasting import lead_time as lt

    assert lt.trimmed_mean([1.0, 2.0, 3.0, 4.0, 100.0], 0.2) == 3.0
    assert lt.trimmed_mean([5.0, 7.0], 0.2) == 6.0


def test_lead_time_evidence_reports_sample_count_and_basis(monkeypatch):
    from app.services.forecasting import lead_time as lt

    monkeypatch.setattr(
        lt.repo, "producer_lead_times", lambda pid, asof, lo, hi: [10.0, 12.0, 14.0, 16.0, 18.0]
    )
    assert lt.producer_lead_time_evidence(1, "2026-06-15")[2:] == ("empirical", 5)
    monkeypatch.setattr(lt.repo, "producer_lead_times", lambda pid, asof, lo, hi: [10.0, 14.0, 18.0])
    monkeypatch.setattr(lt.repo, "producer_agreement_currency", lambda pid, asof: 2)
    assert lt.producer_lead_time_evidence(1, "2026-06-15") == (18.0, 9.0, "geo", 3)
    assert lt.basis_of("empirical") == "measured"
    assert lt.basis_of("geo") == "default"
    assert lt.basis_of("default") == "default"
    assert lt.basis_of("override") == "override"


@pytest.mark.parametrize(
    ("evidence", "basis"),
    [
        ((21.0, 4.0, "empirical", 5), "measured"),
        ((21.0, 4.0, "empirical_trimmed", 9), "measured"),
        ((18.0, 9.0, "geo", 2), "default"),
    ],
)
def test_plan_payload_labels_the_lead_time_basis(monkeypatch, evidence, basis):
    from app.core.config import get_settings

    settings = get_settings().model_copy(update={"use_masters": False, "use_feedback": False})
    monkeypatch.setattr(policy, "get_settings", lambda: settings)
    monkeypatch.setattr(policy.lead_time_svc, "producer_lead_time_evidence", lambda *a: evidence)
    monkeypatch.setattr(policy.repo, "on_hand", lambda *a: {1: 20.0})
    monkeypatch.setattr(policy.repo, "reserved", lambda *a: {})
    monkeypatch.setattr(policy.repo, "on_order", lambda *a: {})
    monkeypatch.setattr(policy.repo, "product_daily_demand_bulk", lambda *a: {})
    monkeypatch.setattr(policy.cost_repo, "producer_unit_costs_eur", lambda *a, **k: {})
    monkeypatch.setattr(policy.cost_repo, "sale_prices_eur", lambda *a: {})
    monkeypatch.setattr(policy.cost_repo, "cheapest_alt_eur", lambda *a, **k: {})
    monkeypatch.setattr(policy.repo, "product_meta", lambda *a: {})
    plan = policy.build_plan(
        99,
        "2026-09-07",
        only_needed=False,
        abc_map={1: "C"},
        producer_name="Test",
        cost_cohort=make_cohort([1], as_of="2026-09-07", costs={(99, 1): [None]}),
        product_ids=[1],
    ).model_dump(mode="json")
    assert plan["lead_time_days"] == evidence[0]
    assert plan["lead_time_source"] == ("geo" if basis == "default" else "empirical")
    assert plan["lead_time_statistic"] == {
        "empirical": "mean", "empirical_trimmed": "trimmed_mean", "geo": None
    }[evidence[2]]
    assert plan["lead_time_basis"] == basis
    assert plan["lead_time_sample_count"] == evidence[3]


def test_seasonal_index_shrinks_and_bounds():
    from app.services.forecasting import demand as d

    assert d.seasonal_index_for([{"d": "2026-01-15", "units": 10}], 1, min_months=12) == 1.0
    rows = [
        {"d": f"{y}-{m:02d}-15", "units": (100 if m == 6 else 10)} for y in (2024, 2025) for m in range(1, 13)
    ]
    idx_jun = d.seasonal_index_for(rows, 6, k=4, lo=0.6, hi=1.6, min_months=12)
    idx_jan = d.seasonal_index_for(rows, 1, k=4, lo=0.6, hi=1.6, min_months=12)
    assert idx_jun > idx_jan
    assert 0.6 <= idx_jan <= idx_jun <= 1.6


@pytest.mark.parametrize(
    "reserved,on_order,position,expected_qty",
    [
        (0, 0, 20, 0),
        (15, 0, 5, 35),
        (15, 8, 13, 0),
        (25, 0, -5, 45),
    ],
)
def test_reservations_and_incoming_supply_affect_actual_plan(
    monkeypatch, reserved, on_order, position, expected_qty
):
    from app.core.config import get_settings

    settings = get_settings().model_copy(
        update={
            "use_masters": False,
            "use_feedback": False,
            "seasonality_enabled": False,
            "economic_service_level": False,
            "forecast_horizon_days": 30,
        }
    )
    monkeypatch.setattr(policy, "get_settings", lambda: settings)
    monkeypatch.setattr(policy.lead_time_svc, "producer_lead_time_evidence", lambda *a: (10.0, 0.0, "test", 0))
    monkeypatch.setattr(policy.repo, "products_for_producer", lambda *a: [1])
    monkeypatch.setattr(policy.repo, "on_hand", lambda *a: {1: 20.0})
    monkeypatch.setattr(policy.repo, "reserved", lambda *a: {1: float(reserved)})
    monkeypatch.setattr(policy.repo, "on_order", lambda *a: {1: float(on_order)})
    monkeypatch.setattr(policy.repo, "product_daily_demand_bulk", lambda *a: {})
    monkeypatch.setattr(policy.cost_repo, "producer_unit_costs_eur", lambda *a, **k: {})
    monkeypatch.setattr(policy.cost_repo, "sale_prices_eur", lambda *a: {})
    monkeypatch.setattr(policy.cost_repo, "cheapest_alt_eur", lambda *a, **k: {})
    monkeypatch.setattr(
        policy.demand_svc,
        "forecast_from_rows",
        lambda *a, **kw: DemandForecast(
            product_id=1, mean_daily=1.0, std_daily=0.0, method="test", horizon_days=30, forecast_units=30.0
        ),
    )
    monkeypatch.setattr(policy.repo, "product_meta", lambda *a: {})
    plan = policy.build_plan(
        99,
        "2026-09-07",
        only_needed=False,
        abc_map={1: "C"},
        producer_name="Test",
        cost_cohort=make_cohort([1], as_of="2026-09-07", costs={(99, 1): [None]}),
    )
    item = plan.items[0]
    assert item.inventory.position == position
    assert item.inventory.available == 20 - reserved
    assert item.suggested_qty == expected_qty
