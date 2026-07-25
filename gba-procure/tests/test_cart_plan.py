"""Unit tests for cart replenishment — mocked repo + per-producer plan, no DB."""
from __future__ import annotations

from app.domain.models import (
    DemandForecast,
    InventoryPosition,
    ProducerPurchasePlan,
    ReorderSuggestion,
    Urgency,
)
from app.services.replenishment import policy


def _sug(product_id: int, producer_id: int, qty: float, urgency: Urgency, cover: float) -> ReorderSuggestion:
    fc = DemandForecast(product_id=product_id, mean_daily=1.0, std_daily=0.0, method="t",
                        horizon_days=30, forecast_units=30.0)
    inv = InventoryPosition(product_id=product_id, on_hand=0, reserved=0, on_order=0,
                            available=0, position=0)
    return ReorderSuggestion(
        product_id=product_id, producer_id=producer_id, suggested_qty=qty,
        reorder_point=10.0, safety_stock=2.0, days_of_cover=cover,
        urgency=urgency, forecast=fc, inventory=inv, reason="t",
    )


def test_build_cart_plan_flattens_sorts_and_filters(monkeypatch):
    monkeypatch.setattr(policy.repo, "all_producers", lambda as_of, history_days: [10, 20])

    plans = {
        10: ProducerPurchasePlan(
            producer_id=10, lead_time_days=5.0, item_count=2,
            items=[
                _sug(1, 10, 4.0, Urgency.NORMAL, 12.0),
                _sug(2, 10, 0.0, Urgency.NONE, 99.0),
            ],
        ),
        20: ProducerPurchasePlan(
            producer_id=20, lead_time_days=5.0, item_count=2,
            items=[
                _sug(3, 20, 6.0, Urgency.HIGH, 3.0),
                _sug(4, 20, 9.0, Urgency.HIGH, 1.0),
            ],
        ),
    }
    monkeypatch.setattr(policy.classify_svc, "get_abc_map", lambda as_of, history_days: {})
    monkeypatch.setattr(policy.repo, "producer_names", lambda ids: {})
    monkeypatch.setattr(policy, "build_plan",
                        lambda pid, as_of, only_needed=True, abc_map=None, producer_name=None: plans[pid])

    plan = policy.build_cart_plan("2026-06-15", only_needed=True, limit=200)

    assert plan.item_count == 3
    assert [i.product_id for i in plan.items] == [4, 3, 1]
    assert all(i.suggested_qty > 0 for i in plan.items)
    assert plan.as_of_date == "2026-06-15"
    assert plan.model_version == "procure-hist120-v1"


def test_build_cart_plan_budget_knapsack(monkeypatch):
    a = _sug(1, 10, 5.0, Urgency.CRITICAL, 1.0)
    a.line_cost_eur = 100.0
    a.unit_margin_eur = 4.0
    b = _sug(2, 10, 5.0, Urgency.HIGH, 1.0)
    b.line_cost_eur = 50.0
    b.unit_margin_eur = 2.0
    c = _sug(3, 10, 5.0, Urgency.CRITICAL, 1.0)
    c.line_cost_eur = 80.0
    c.unit_margin_eur = 10.0
    plan_obj = ProducerPurchasePlan(producer_id=10, lead_time_days=5.0,
                                    items=[a, b, c], item_count=3)
    monkeypatch.setattr(policy.repo, "all_producers", lambda as_of, history_days: [10])
    monkeypatch.setattr(policy.classify_svc, "get_abc_map", lambda as_of, history_days: {})
    monkeypatch.setattr(policy.repo, "producer_names", lambda ids: {})
    monkeypatch.setattr(policy, "build_plan",
                        lambda pid, as_of, only_needed=True, abc_map=None, producer_name=None: plan_obj)

    plan = policy.build_cart_plan("2026-06-15", budget_eur=130.0)

    assert plan.selected_count == 2
    assert plan.budget_used_eur == 130.0
    assert {it.product_id for it in plan.items if it.within_budget} == {3, 2}
    assert plan.items[0].product_id == 3


def test_milp_finds_optimal_knapsack_beating_greedy():
    from app.services.optimization import milp
    values = [6.0, 5.0, 5.0]
    costs = [6.0, 5.0, 5.0]
    chosen, used_milp = milp.select_within_budget(values, costs, 10.0)
    assert used_milp is True
    assert sum(values[i] for i in chosen) == 10.0
    assert chosen == {1, 2}
    greedy = milp._greedy(values, costs, 10.0)
    assert sum(values[i] for i in greedy) == 6.0


def test_milp_never_labels_an_unsolved_model_as_exact():
    import inspect

    from app.services.optimization import milp

    src = inspect.getsource(milp.select_within_budget)
    assert 'LpStatus[status] == "Optimal"' in src
    assert '"Not Solved"' not in src


def test_build_cart_plan_respects_limit(monkeypatch):
    monkeypatch.setattr(policy.repo, "all_producers", lambda as_of, history_days: [10])
    plan_obj = ProducerPurchasePlan(
        producer_id=10, lead_time_days=5.0, item_count=3,
        items=[
            _sug(1, 10, 5.0, Urgency.HIGH, 2.0),
            _sug(2, 10, 5.0, Urgency.HIGH, 3.0),
            _sug(3, 10, 5.0, Urgency.HIGH, 4.0),
        ],
    )
    monkeypatch.setattr(policy.classify_svc, "get_abc_map", lambda as_of, history_days: {})
    monkeypatch.setattr(policy.repo, "producer_names", lambda ids: {})
    monkeypatch.setattr(policy, "build_plan",
                        lambda pid, as_of, only_needed=True, abc_map=None, producer_name=None: plan_obj)

    plan = policy.build_cart_plan("2026-06-15", only_needed=True, limit=2)

    assert plan.item_count == 2
    assert [i.product_id for i in plan.items] == [1, 2]


def test_build_cart_plan_includes_zero_quantity_items_when_requested(monkeypatch):
    monkeypatch.setattr(policy.repo, "all_producers", lambda as_of, history_days: [10])
    plan_obj = ProducerPurchasePlan(
        producer_id=10,
        lead_time_days=5.0,
        item_count=2,
        items=[
            _sug(1, 10, 5.0, Urgency.HIGH, 2.0),
            _sug(2, 10, 0.0, Urgency.NONE, 99999.0),
        ],
    )
    monkeypatch.setattr(policy.classify_svc, "get_abc_map", lambda as_of, history_days: {})
    monkeypatch.setattr(policy.repo, "producer_names", lambda ids: {})
    monkeypatch.setattr(
        policy,
        "build_plan",
        lambda pid, as_of, only_needed=True, abc_map=None, producer_name=None: plan_obj,
    )

    plan = policy.build_cart_plan("2026-06-15", only_needed=False, limit=-1)

    assert plan.item_count == 2
    assert [(i.product_id, i.suggested_qty) for i in plan.items] == [(1, 5.0), (2, 0.0)]


def test_build_cart_plan_keeps_one_cheapest_supplier_option_per_product(monkeypatch):
    expensive = _sug(1, 10, 5.0, Urgency.HIGH, 2.0)
    expensive.unit_cost_eur = 12.0
    expensive.line_cost_eur = 60.0
    cheapest = _sug(1, 20, 7.0, Urgency.HIGH, 3.0)
    cheapest.unit_cost_eur = 10.0
    cheapest.line_cost_eur = 70.0
    unique = _sug(2, 20, 3.0, Urgency.NORMAL, 10.0)
    unique.unit_cost_eur = 2.675
    unique.line_cost_eur = 8.03
    plans = {
        10: ProducerPurchasePlan(
            producer_id=10,
            lead_time_days=5.0,
            item_count=1,
            items=[expensive],
        ),
        20: ProducerPurchasePlan(
            producer_id=20,
            lead_time_days=5.0,
            item_count=2,
            items=[cheapest, unique],
        ),
    }
    monkeypatch.setattr(policy.repo, "all_producers", lambda as_of, history_days: [10, 20])
    monkeypatch.setattr(policy.classify_svc, "get_abc_map", lambda as_of, history_days: {})
    monkeypatch.setattr(policy.repo, "producer_names", lambda ids: {})
    monkeypatch.setattr(
        policy,
        "build_plan",
        lambda pid, as_of, only_needed=True, abc_map=None, producer_name=None: plans[pid],
    )

    plan = policy.build_cart_plan("2026-06-15", only_needed=True, limit=-1)

    assert {(i.product_id, i.producer_id) for i in plan.items} == {(1, 20), (2, 20)}
    assert plan.duplicate_supplier_options_removed == 1
    assert plan.total_item_count == 2
    assert plan.total_suggested_qty == 10.0
    assert plan.total_cost_eur == 78.03
    assert plan.priced_cost_eur == 78.03
    assert plan.unpriced_item_count == 0
