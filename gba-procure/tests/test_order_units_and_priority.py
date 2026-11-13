"""Whole sale units, evidence-aware urgency and the uncertified cost estimate."""

import pytest
from test_purchase_cost_cohort import make_cohort

from app.domain.models import DemandForecast, InventoryPosition, ReorderSuggestion, Urgency
from app.services.replenishment import policy

AS_OF = "2026-09-08"


@pytest.mark.parametrize(
    "qty,unit,expected",
    [
        (0.93, "шт", 1.0),
        (3.0, "шт", 3.0),
        (3.0000001, "компл", 3.0),
        (3.01, None, 4.0),
        (0.93, "кг", 0.93),
        (12.4, "м.кв", 12.4),
        (2.5, " Л ", 2.5),
        (0.0, "шт", 0.0),
    ],
)
def test_whole_units_round_up_only_piece_measured_goods(qty, unit, expected):
    assert policy._whole_units(qty, unit) == expected


def _suggestion(urgency, lead_demand):
    inv = InventoryPosition(product_id=1, on_hand=0, reserved=0, on_order=0, available=0, position=0)
    forecast = DemandForecast(product_id=1, mean_daily=0.1, std_daily=0, forecast_units=3)
    return ReorderSuggestion(
        product_id=1, producer_id=7, suggested_qty=1, reorder_point=1, safety_stock=0,
        lead_demand=lead_demand, days_of_cover=0, urgency=urgency, forecast=forecast,
        inventory=inv, reason="fixture",
    )


@pytest.mark.parametrize(
    "base,abc,lead_demand,evidence,expected",
    [
        (Urgency.CRITICAL, "A", 1.0, "regular", Urgency.CRITICAL),
        (Urgency.CRITICAL, "A", 0.99, "regular", Urgency.HIGH),
        (Urgency.CRITICAL, "B", 5.0, "regular", Urgency.HIGH),
        (Urgency.CRITICAL, "C", 1.0, "regular", Urgency.HIGH),
        (Urgency.CRITICAL, "C", 0.5, "regular", Urgency.NORMAL),
        (Urgency.HIGH, "A", 0.1, "regular", Urgency.HIGH),
        (Urgency.HIGH, "B", 9.0, "regular", Urgency.NORMAL),
        (Urgency.CRITICAL, "A", 9.0, "on_request", Urgency.NORMAL),
        (Urgency.NORMAL, "A", 9.0, "regular", Urgency.NORMAL),
        (Urgency.NONE, "A", 9.0, "regular", Urgency.NONE),
    ],
)
def test_priority_urgency_rule(base, abc, lead_demand, evidence, expected):
    assert policy._priority_urgency(_suggestion(base, lead_demand), abc, evidence) == expected


def test_thin_demand_is_one_unit_or_less_in_the_window():
    assert policy._demand_evidence([]) == "on_request"
    assert policy._demand_evidence([{"units": 1}]) == "on_request"
    assert policy._demand_evidence([{"units": 0.5}, {"units": 0.6}]) == "regular"
    assert policy._demand_evidence([{"units": 2}]) == "regular"


@pytest.fixture
def sources(monkeypatch):
    repo = policy.repo
    demand = {1: 40, 2: 1, 3: 40, 4: 40}
    monkeypatch.setattr(repo, "all_producers", lambda *a: [7])
    monkeypatch.setattr(repo, "producer_names", lambda *a: {7: "Fixture"})
    monkeypatch.setattr(repo, "producer_name", lambda *a: "Fixture")
    monkeypatch.setattr(repo, "on_hand", lambda *a: {})
    monkeypatch.setattr(repo, "reserved", lambda *a: {})
    monkeypatch.setattr(repo, "on_order", lambda *a: {})
    monkeypatch.setattr(repo, "product_meta", lambda *a: {})
    monkeypatch.setattr(
        repo,
        "product_daily_demand_bulk",
        lambda ids, *a: {pid: [{"d": "2026-09-01", "units": demand[pid]}] for pid in ids},
    )
    monkeypatch.setattr(repo, "product_measure_units", lambda ids: {1: "шт", 2: "шт", 3: "кг", 4: "шт"})
    monkeypatch.setattr(
        repo, "last_document_costs_eur", lambda *a, **k: {(7, 1): 99.0, (7, 2): 3.33333, (7, 3): 2.0}
    )
    monkeypatch.setattr(policy.lead_time_svc, "producer_lead_time_evidence", lambda *a: (10, 0, "fixture", 0))
    monkeypatch.setattr(policy.classify_svc, "get_abc_map", lambda *a: {1: "A", 2: "A", 3: "B"})
    monkeypatch.setattr(policy.masters, "producer_profile", lambda *a, **k: None)
    monkeypatch.setattr(
        policy.masters, "product_terms_for", lambda *a, **k: {4: {"moq": 2.5, "order_multiple": 1.5}}
    )
    monkeypatch.setattr(policy.feedback, "learned_factors", lambda *a: {})
    monkeypatch.setattr(policy.cost_repo, "sale_prices_eur", lambda *a: {})
    monkeypatch.setattr(
        policy.demand_svc,
        "forecast_from_rows",
        lambda pid, rows, *a, **k: DemandForecast(
            product_id=pid, mean_daily=demand[pid] / 120, std_daily=0, forecast_units=demand[pid] / 4
        ),
    )
    return make_cohort([1, 2, 3, 4], costs={(7, 1): ["5"], (7, 2): [None], (7, 3): [None], (7, 4): [None]})


@pytest.mark.order_units
def test_plan_rounds_pieces_marks_thin_demand_and_estimates_unpriced_lines(sources):
    plan = policy.build_plan(7, AS_OF, cost_cohort=sources, product_ids=[1, 2, 3, 4])
    items = {item.product_id: item for item in plan.items}

    assert items[1].suggested_qty == 14 and items[1].raw_qty == 13.33
    assert items[1].measure_unit == "шт" and items[1].demand_evidence == "regular"
    assert items[1].urgency == Urgency.CRITICAL
    assert items[1].cost_source == "receipt_history"
    assert items[1].unit_cost_eur == 5 and items[1].line_cost_eur == 70
    assert items[1].estimated_unit_cost_eur == 5 and items[1].estimated_line_cost_eur == 70

    assert items[2].suggested_qty == 1 and items[2].raw_qty == 0.33
    assert items[2].demand_evidence == "on_request" and items[2].urgency == Urgency.NORMAL
    assert items[2].budget_priority_weight == 0.4
    assert items[2].unit_cost_eur is None and items[2].line_cost_eur is None
    assert items[2].cost_source == "last_receipt_fallback"
    assert items[2].estimated_unit_cost_eur == 3.3333 and items[2].estimated_line_cost_eur == 3.33
    assert items[2].cost_provenance.source == "receipt_history"
    assert not items[2].cost_provenance.budget_eligible

    assert items[3].suggested_qty == 13.33 and items[3].raw_qty is None
    assert items[3].urgency == Urgency.HIGH
    assert items[3].estimated_line_cost_eur == 26.66

    assert items[4].suggested_qty == 15 and items[4].moq == 2.5 and items[4].order_multiple == 1.5
    assert items[4].cost_source == "none" and items[4].estimated_line_cost_eur is None
    assert items[4].urgency == Urgency.HIGH


@pytest.mark.order_units
def test_cart_keeps_certified_totals_and_adds_the_estimate_split(sources):
    cart = policy.build_cart_plan(AS_OF, cost_cohort=sources, products_by_producer={7: [1, 2, 3, 4]})
    assert cart.unpriced_item_count == 3 and cart.total_cost_eur is None
    assert cart.priced_cost_eur == 70 and cart.certified_cost_eur == 70
    assert cart.uncertified_cost_eur == 29.99
    assert cart.estimated_total_cost_eur == 99.99
    assert cart.unestimated_item_count == 1
    assert not cart.cost_totals_certified
    assert cart.total_suggested_qty == 14 + 1 + 13.33 + 15


@pytest.mark.order_units
def test_cart_reports_recommended_quantity_per_measure_unit(sources):
    cart = policy.build_cart_plan(AS_OF, cost_cohort=sources, products_by_producer={7: [1, 2, 3, 4]})
    assert cart.suggested_qty_by_unit == {"кг": 13.33, "шт": 30.0}
    assert sum(cart.suggested_qty_by_unit.values()) == cart.total_suggested_qty


@pytest.mark.order_units
def test_buyer_priced_lines_are_not_reported_as_receipt_certified(sources, monkeypatch):
    monkeypatch.setattr(
        policy.masters,
        "product_terms_for",
        lambda *a, **k: {4: {"moq": 2.5, "order_multiple": 1.5, "unit_cost_override": 2.0}},
    )
    cart = policy.build_cart_plan(AS_OF, cost_cohort=sources, products_by_producer={7: [1, 2, 3, 4]})
    items = {item.product_id: item for item in cart.items}
    assert items[4].line_cost_eur == 30 and not items[4].cost_provenance.budget_eligible
    assert cart.priced_cost_eur == 100
    assert cart.certified_cost_eur == 70
    assert cart.uncertified_cost_eur == 59.99
    assert cart.estimated_total_cost_eur == 129.99
