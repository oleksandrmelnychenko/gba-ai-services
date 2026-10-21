"""Independent arithmetic boundaries; fixtures never access SQL/Mongo/Redis."""

import json
from datetime import date
from decimal import Decimal

import pytest
from native_cost_fixture import make_cohort, prepare_plan_stubs

from app.core.config import Settings
from app.domain.models import (
    DemandForecast,
    InventoryPosition,
    ProducerPurchasePlan,
    ReorderSuggestion,
    Urgency,
)
from app.services.forecasting import demand
from app.services.optimization import milp
from app.services.replenishment import policy


def _item(pid, cost):
    return ReorderSuggestion(
        product_id=pid,
        producer_id=10,
        suggested_qty=1,
        reorder_point=1,
        safety_stock=0,
        days_of_cover=0,
        urgency=Urgency.CRITICAL,
        reason="fixture",
        forecast=DemandForecast(product_id=pid, mean_daily=1, std_daily=0, forecast_units=30),
        inventory=InventoryPosition(product_id=pid),
        unit_cost_eur=cost,
        line_cost_eur=cost,
        unit_margin_eur=1,
    )


@pytest.mark.parametrize("method", ["greedy", "milp"])
@pytest.mark.parametrize("budget, count, spend", [(0.3, 2, 0.3), (0.2999, 1, 0.1)])
def test_cart_exact_budget_without_rounding_budget_up(monkeypatch, method, budget, count, spend):
    plan = ProducerPurchasePlan(
        producer_id=10,
        lead_time_days=1,
        items=[_item(1, 0.1), _item(2, 0.2)],
        item_count=2,
    )
    monkeypatch.setattr(policy.repo, "all_producers", lambda *a: [10])
    monkeypatch.setattr(policy.repo, "producer_names", lambda *a: {})
    monkeypatch.setattr(policy.classify_svc, "get_abc_map", lambda *a: {})
    monkeypatch.setattr(policy, "_cart_producer_plan", lambda *a, **k: plan)
    prepare_plan_stubs(monkeypatch, {10: plan}, "2026-09-08")
    result = policy.build_cart_plan("2026-09-08", budget_eur=budget, method=method)
    assert result.selected_count == count
    # MILP may choose either one of the equally valuable lines below the full budget.
    if budget == 0.3 or method == "greedy":
        assert result.budget_used_eur == spend
    selected = [Decimal(str(i.line_cost_eur)) for i in result.items if i.within_budget]
    assert sum(selected, Decimal()) <= Decimal(str(budget))
    assert Decimal(str(result.budget_used_eur)) == sum(selected, Decimal())


@pytest.mark.parametrize(
    "costs,budget,expected",
    [
        ([0.1, 0.2], 0.3, {0, 1}),
        ([0.1, 0.2], 0.2999, {0}),
        ([0.001, 0.002], 0.003, {0, 1}),
        ([0.001, 0.002], 0.002999, {0}),
    ],
)
def test_both_optimizer_paths_preserve_exact_input_units(costs, budget, expected):
    assert milp._greedy([1, 1], costs, budget) == expected
    chosen, exact = milp.select_within_budget([1, 1], costs, budget)
    assert exact
    assert len(chosen) == len(expected)
    assert sum((Decimal(str(costs[i])) for i in chosen), Decimal()) <= Decimal(str(budget))


@pytest.mark.parametrize(
    "qty,moq,multiple,expected",
    [
        (2.1, None, 1, 3.0),
        (2.1, None, 0.5, 2.5),
        (0.02, None, 0.003, 0.021),
        (0.01, 0.02, 0.003, 0.021),
        (0.021, None, 0.003, 0.021),
        (0, 10, 0.003, 0),
    ],
)
def test_every_positive_pack_is_enforced_without_cent_quantization(qty, moq, multiple, expected):
    result = policy._round_order_qty(qty, moq, multiple)
    assert result == expected
    if result > 0:
        assert Decimal(str(result)) % Decimal(str(multiple)) == 0
        assert result >= max(qty, moq or 0)


def test_actual_plan_and_json_keep_fractional_pack(monkeypatch):
    cfg = Settings(
        _env_file=None,
        use_masters=True,
        use_feedback=False,
        seasonality_enabled=False,
        economic_service_level=False,
        forecast_horizon_days=1,
    )
    monkeypatch.setattr(policy, "get_settings", lambda: cfg)
    monkeypatch.setattr(policy.lead_time_svc, "producer_lead_time", lambda *a: (1.0, 0.0, "fixture"))
    monkeypatch.setattr(policy.masters, "producer_profile", lambda *a, **k: None)
    monkeypatch.setattr(policy.masters, "product_terms_for", lambda *a, **k: {1: {"order_multiple": 0.003}})
    monkeypatch.setattr(policy.repo, "products_for_producer", lambda *a: [1])
    for name in ["on_hand", "reserved", "on_order", "product_daily_demand_bulk", "product_meta"]:
        monkeypatch.setattr(policy.repo, name, lambda *a: {})
    monkeypatch.setattr(policy.cost_repo, "producer_unit_costs_eur", lambda *a, **k: {1: 100.0})
    monkeypatch.setattr(policy.cost_repo, "sale_prices_eur", lambda *a: {})
    monkeypatch.setattr(policy.cost_repo, "cheapest_alt_eur", lambda *a, **k: {})
    monkeypatch.setattr(
        policy.demand_svc,
        "forecast_from_rows",
        lambda *a, **k: DemandForecast(
            product_id=1, mean_daily=0.01, std_daily=0, horizon_days=1, forecast_units=0.01
        ),
    )
    result = policy.build_plan(
        10,
        "2026-09-08",
        abc_map={1: "C"},
        producer_name="Fixture",
        cost_cohort=make_cohort([1], costs={(10, 1): ["100"]}),
    )
    row = json.loads(result.model_dump_json())["items"][0]
    assert row["suggested_qty"] == 0.021
    assert row["line_cost_eur"] == 2.1
    assert Decimal(str(row["suggested_qty"])) % Decimal(str(row["order_multiple"])) == 0


@pytest.mark.parametrize("sba", [False, True])
def test_croston_initial_interval_is_number_of_observed_periods(sba):
    rows = [{"d": "2026-01-03", "units": 10}, {"d": "2026-01-05", "units": 20}]
    # Dense days Jan1..6 are 0,0,10,0,20,0; occurrence periods3 and5.
    z = Decimal(10) + Decimal(".1") * (Decimal(20) - Decimal(10))
    interval = Decimal(3) + Decimal(".1") * (Decimal(2) - Decimal(3))
    expected = z / interval * (Decimal(".95") if sba else Decimal(1))
    assert demand._croston_rate(rows, 6, 0.1, sba, date(2026, 1, 1)) == pytest.approx(float(expected))


def test_first_day_demand_still_starts_at_interval_one():
    rows = [{"d": "2026-01-01", "units": 10}, {"d": "2026-01-03", "units": 20}]
    assert demand._croston_rate(rows, 6, 0.1, False, date(2026, 1, 1)) == pytest.approx(11 / 1.1)


def test_cart_total_keeps_pack_precision():
    a, b = _item(1, 1), _item(2, 1)
    a.suggested_qty = b.suggested_qty = 0.021
    assert policy._cart_totals([a, b])["total_suggested_qty"] == 0.042


def test_milp_declines_unrepresentable_integer_coefficients():
    chosen, exact = milp.select_within_budget([1], [float(2**54)], float(2**54))
    assert chosen == {0}
    assert exact is False


def test_tiny_positive_cost_cannot_disappear_after_large_spend():
    # Give the large item higher density so it is selected first.
    assert milp._greedy([1e40, 1e-30], [1e20, 1e-20], 1e20) == {0}
    chosen, exact = milp.select_within_budget([1e40, 1e-30], [1e20, 1e-20], 1e20)
    assert chosen == {0}
    assert exact is False


def test_cart_cannot_certify_a_sum_that_lost_a_quantity_contribution():
    a, b = _item(1, 1), _item(2, 1)
    a.suggested_qty = 1e20
    b.suggested_qty = 1e-20
    with pytest.raises(ValueError, match="exact numeric range"):
        policy._cart_totals([a, b])


def test_cart_refuses_unrepresentable_aggregate_cents_instead_of_authorizing_budget(monkeypatch):
    # Each line is an exact supported price and cent amount; their sum is not a JSON scalar.
    a, b = _item(1, 1e15), _item(2, 0.01)
    plan = ProducerPurchasePlan(producer_id=10, lead_time_days=1, items=[a, b], item_count=2)
    monkeypatch.setattr(policy.repo, "all_producers", lambda *a: [10])
    monkeypatch.setattr(policy.repo, "producer_names", lambda *a: {})
    monkeypatch.setattr(policy.classify_svc, "get_abc_map", lambda *a: {})
    monkeypatch.setattr(policy, "_cart_producer_plan", lambda *a, **k: plan)
    prepare_plan_stubs(monkeypatch, {10: plan}, "2026-09-08")
    with pytest.raises(ValueError, match="exact numeric range"):
        policy.build_cart_plan("2026-09-08", budget_eur=1e15, method="greedy")
