"""Offline independent cohort and budget semantics; all external paths are mocked."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal, localcontext

import pytest
from native_cost_fixture import make_cohort

from app.data.native_purchase_costs import NativePurchaseCostError
from app.data.purchase_cost_cohort import buyer_provenance, exact_line_amount, read_purchase_cost_cohort
from app.domain.models import DemandForecast, Urgency
from app.services.replenishment import policy


def test_one_build_cohort_is_reused_across506_supplier_views_and_alt_prices():
    fixture = make_cohort(range(1, 1002), costs={(7, 1): ["10"], (8, 1): ["9"]})

    class Reader:
        requests = []

        def read(self, request):
            self.requests.append(request)
            return next(snapshot for snapshot in fixture.snapshots if snapshot.request == request)

    reader = Reader()
    current = read_purchase_cost_cohort(list(range(1, 1002)), "2026-09-08", client=reader)
    assert [len(item.product_ids) for item in reader.requests] == [1000, 1]
    for supplier in range(1, 507):
        current.group(1, supplier)
        current.provenance(1, supplier, 3)
        current.supplier_groups(1)
    assert len(reader.requests) == 2
    assert current.manifest().snapshot_relationship == "separate_snapshots"
    assert current.manifest([1]).components[0].product_ids == list(range(1, 1001))
    assert current.provenance(1, 7, 3).supplier_client_agreement_ids == [7001]


def test_partial_history_visible_but_zero_only_eligible_with_complete_proof():
    cohort = make_cohort([1, 2, 3], costs={(7, 1): ["0"], (7, 2): ["0", None]})
    zero = cohort.provenance(1, 7, 100)
    partial = cohort.provenance(2, 7, 100)
    missing = cohort.provenance(3, 7, 100)
    assert zero.budget_eligible and zero.canonical_unit_cost_eur == "0.0000"
    assert not partial.budget_eligible and partial.coverage == "partial"
    assert partial.canonical_unit_cost_eur == "0.0000" and partial.unknown_count == 1
    assert not missing.budget_eligible and missing.coverage == "no_observations"
    assert missing.canonical_unit_cost_eur is None


def test_buyer_override_never_inherits_receipt_or_verified_tax_basis():
    metadata = buyer_provenance(12.345678)
    assert metadata.canonical_unit_cost_eur == "12.345678"
    assert metadata.source == "buyer_supplied" and not metadata.budget_eligible
    assert metadata.component_refs == [] and metadata.supplier_client_agreement_ids == []
    assert metadata.observation_count == 0 and metadata.resolver_version is None


def test_line_extension_uses_fraction_before_cent_rounding_and_refuses_float_loss():
    with localcontext() as context:
        context.prec = 2
        exact, value = exact_line_amount(Decimal("0.335"), Decimal("3"))
        huge, missing = exact_line_amount(Decimal("900000000000000.0001"), Decimal("100"))
    assert exact == Decimal("1.01") and value == 1.01
    assert huge == Decimal("90000000000000000.01") and missing is None


def test_fresh_financial_fingerprint_change_is_not_hidden_by_same_receipt_metadata():
    cohort = make_cohort([1], costs={(7, 1): ["10"]})

    class Changed:
        def read(self, request):
            return replace(cohort.snapshots[0], fingerprint="d" * 64)

    assert not cohort.reread_unchanged(Changed())
    with pytest.raises(NativePurchaseCostError):
        cohort.require_scope([2], "2026-09-08")


@pytest.fixture
def plan_input(monkeypatch):
    products = [1, 2, 3, 4, 5]
    cohort = make_cohort(
        products, costs={(7, 1): ["0"], (7, 2): ["100"], (7, 3): ["10", None], (7, 4): ["10"], (7, 5): [None]}
    )
    monkeypatch.setattr(policy.repo, "all_producers", lambda *args: [7])
    monkeypatch.setattr(policy.repo, "producer_names", lambda *args: {7: "Fixture"})
    monkeypatch.setattr(policy.repo, "on_hand", lambda *args: {})
    monkeypatch.setattr(policy.repo, "reserved", lambda *args: {})
    monkeypatch.setattr(policy.repo, "on_order", lambda *args: {})
    monkeypatch.setattr(policy.repo, "product_daily_demand_bulk", lambda *args: {})
    monkeypatch.setattr(policy.repo, "product_meta", lambda *args: {})
    monkeypatch.setattr(policy.lead_time_svc, "producer_lead_time", lambda *args: (10, 0, "fixture"))
    monkeypatch.setattr(policy.classify_svc, "get_abc_map", lambda *args: {})
    monkeypatch.setattr(policy.masters, "producer_profile", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        policy.masters, "product_terms_for", lambda *args, **kwargs: {4: {"unit_cost_override": 1}}
    )
    monkeypatch.setattr(policy.feedback, "learned_factors", lambda *args: {})
    monkeypatch.setattr(policy.cost_repo, "sale_prices_eur", lambda *args: dict.fromkeys(products, 120))
    monkeypatch.setattr(
        policy.demand_svc,
        "forecast_from_rows",
        lambda pid, *args, **kwargs: DemandForecast(
            product_id=pid, mean_daily=1, std_daily=0, forecast_units=30
        ),
    )
    monkeypatch.setattr(policy.repo, "producer_name", lambda *args: "Fixture")

    def forbidden(*args, **kwargs):
        raise AssertionError("unproved gross-sale versus net-cost economic calculation")

    monkeypatch.setattr(policy, "_economic_service_level", forbidden)
    return cohort, {7: products}


@pytest.mark.parametrize("method", ["greedy", "milp"])
def test_budget_uses_dimensionless_urgency_not_gross_margin_and_excludes_partial_buyer(plan_input, method):
    cohort, products = plan_input
    plan = policy.build_cart_plan(
        "2026-09-08", budget_eur=1, method=method, cost_cohort=cohort, products_by_producer=products
    )
    assert {item.product_id for item in plan.items if item.within_budget} == {1}
    assert plan.budget_used_eur == 0 and plan.budget_score == 1
    assert plan.budget_objective == "urgency_weighted_lines" and plan.value_captured_eur is None
    assert all(item.unit_margin_eur is None and item.value_density is None for item in plan.items)
    assert all(item.sale_price_basis == "historical_recorded_tax_basis_unverified" for item in plan.items)
    assert not plan.cost_totals_certified
    assert plan.cost_total_basis == "includes_buyer_values_with_unverified_tax_basis"
    assert plan.history_scope == "demand" and "purchase_costs" in plan.history_not_applicable


def test_zero_budget_selects_only_verified_free_lines_and_score_sums_exact_tenths(plan_input):
    cohort, products = plan_input
    plan = policy.build_plan(7, "2026-09-08", cost_cohort=cohort, product_ids=products[7])
    plan.items[0].urgency = Urgency.HIGH
    chosen, _, used, score = policy._select_urgency_lines(plan.items, 0, "milp", 10)
    assert chosen == {0} and used == 0 and score.numerator == 7 and score.denominator == 10


def test_zero_quantity_is_not_a_free_order_and_cannot_inflate_priority(plan_input):
    cohort, products = plan_input
    assert not cohort.provenance(1, 7, 0).budget_eligible
    assert "no_positive_suggested_quantity" in cohort.provenance(1, 7, 0).budget_exclusion_reasons
    plan = policy.build_plan(7, "2026-09-08", cost_cohort=cohort, product_ids=products[7])
    plan.items[0].suggested_qty = 0
    for method in ("greedy", "milp"):
        chosen, _, used, score = policy._select_urgency_lines(plan.items, 0, method, 10)
        assert not chosen and used == 0 and score == 0


def test_chart_manifest_is_underlying_plan_not_topn_and_avoids_cost_reread(plan_input, monkeypatch):
    cohort, products = plan_input
    plan = policy.build_cart_plan("2026-09-08", cost_cohort=cohort, products_by_producer=products)
    monkeypatch.setattr(
        policy, "read_purchase_cost_cohort", lambda *args: pytest.fail("duplicate proof read")
    )
    charts = policy.build_charts(None, "2026-09-08", top_n=1, plan_input=plan)
    assert len(charts.top_items) == 1
    assert charts.cost_observation_manifest == plan.cost_observation_manifest
    assert charts.cost_totals_certified == plan.cost_totals_certified
