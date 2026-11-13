"""DB-backed integration smoke — runs against the live dev DB, skipped without DB env.

Marked `integration` and SKIPPED when DB connection env is absent, so the default CI job
(pytest -q, unit-only) stays green with no DB. Run explicitly via `make integration`
(pytest -m integration) with the read-only login env set:

    DB_HOST=127.0.0.1 DB_PORT=1433 DB_NAME=ConcordDb_V5 \
    DB_USER=gba_reco_ro DB_PASSWORD=... REDIS_DB=1 \
    pytest -m integration

These discover current producers/products through the authoritative candidate query,
so a 1C rekey/re-mint cannot invalidate the smoke merely by changing entity IDs.
"""
from __future__ import annotations

import os
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

import pytest

pytestmark = pytest.mark.integration

_DB_ENV_READY = bool(os.getenv("DB_PASSWORD")) and bool(os.getenv("DB_HOST"))

if not _DB_ENV_READY:
    pytest.skip("DB env not configured (set DB_HOST/DB_PASSWORD); integration smoke skipped",
                allow_module_level=True)

from app.core.config import get_settings  # noqa: E402
from app.data import cost_repository as cost_repo  # noqa: E402
from app.data import supply_repository as repo  # noqa: E402
from app.data.synthetic import synthetic_product_id  # noqa: E402
from app.services.forecasting import lead_time as lead_time_svc  # noqa: E402
from app.services.replenishment import policy  # noqa: E402

AS_OF = date.today().isoformat()
HISTORY_DAYS = get_settings().history_days


@pytest.fixture(scope="module")
def authoritative_candidates():
    """Current real producer/product pairs, discovered only through the production path."""
    producer_ids = repo.all_producers(AS_OF, HISTORY_DAYS)
    assert producer_ids, "authoritative candidate query returned no current producers"

    candidates = []
    for producer_id in producer_ids:
        product_ids = repo.products_for_producer(producer_id, AS_OF, HISTORY_DAYS)
        if product_ids:
            candidates.append((producer_id, product_ids))
    assert candidates, "current producers have no authoritative real-product candidates"
    return sorted(candidates, key=lambda pair: len(pair[1]), reverse=True)


@pytest.fixture(scope="module")
def current_candidate(authoritative_candidates):
    for producer_id, product_ids in authoritative_candidates:
        name = repo.producer_name(producer_id)
        if name:
            return producer_id, name, product_ids
    pytest.fail("authoritative candidates have no resolvable Client.SupplierName")


@pytest.fixture(scope="module")
def current_empirical_candidate(authoritative_candidates):
    for producer_id, product_ids in authoritative_candidates:
        samples = repo.producer_lead_times(producer_id, AS_OF)
        if samples:
            return producer_id, product_ids, samples
    pytest.skip("current authoritative candidates have no completed factual receipt samples")


@pytest.fixture(scope="module")
def current_in_transit_candidate(authoritative_candidates):
    for producer_id, product_ids in authoritative_candidates:
        open_qty = repo.on_order(product_ids, AS_OF)
        if open_qty:
            return producer_id, product_ids, open_qty
    pytest.skip("current authoritative candidates have no in-transit factual supply")


@pytest.fixture(scope="module")
def current_priced_candidate(authoritative_candidates):
    for producer_id, product_ids in authoritative_candidates:
        sample_ids = product_ids[:200]
        costs = cost_repo.producer_unit_costs_eur(producer_id, sample_ids, AS_OF)
        if costs:
            return producer_id, product_ids, costs
    pytest.fail("authoritative candidates have no positive factual supplier prices")


def test_producer_name_resolves_supplier_from_client(current_candidate):
    producer_id, expected_name, _product_ids = current_candidate
    name = repo.producer_name(producer_id)
    assert name == expected_name
    assert name.strip()


def test_producer_lead_time_is_positive_and_sane(current_candidate):
    producer_id, _name, _product_ids = current_candidate
    mean_days, std_days, source = lead_time_svc.producer_lead_time(producer_id, AS_OF)
    assert mean_days > 0
    assert mean_days < 365
    assert std_days >= 0
    assert source in ("empirical", "empirical_trimmed", "geo", "default")


def test_producer_lead_times_samples_within_plausible_window(current_empirical_candidate):
    _producer_id, _product_ids, samples = current_empirical_candidate
    assert samples
    assert all(1 <= s <= 120 for s in samples)


def test_build_plan_returns_real_items_with_sane_cover_and_no_synthetic(current_candidate):
    producer_id, producer_name, product_ids = current_candidate
    plan = policy.build_plan(producer_id, AS_OF, only_needed=False)

    assert plan.producer_name == producer_name
    assert plan.lead_time_days > 0
    assert plan.item_count > 0
    assert plan.item_count == len(plan.items)
    assert {item.product_id for item in plan.items} == set(product_ids)
    assert all(item.days_of_cover >= 0 for item in plan.items)
    assert all(item.product_id != synthetic_product_id() for item in plan.items)


def test_on_order_is_nonempty_for_current_in_transit_supply(current_in_transit_candidate):
    _producer_id, _product_ids, oo = current_in_transit_candidate
    nonzero = {p: q for p, q in oo.items() if q > 0}
    assert nonzero
    assert synthetic_product_id() not in oo
    assert all(q > 0 for q in oo.values())
    assert sum(nonzero.values()) > 0


def test_on_order_nets_receipts_and_only_returns_positive_open_qty(
    current_in_transit_candidate,
):
    _producer_id, product_ids, expected = current_in_transit_candidate
    actual = repo.on_order(product_ids, AS_OF)
    assert actual == expected
    assert all(q > 0 for q in actual.values())


def test_on_order_raises_position_above_available_for_in_transit_items(
    current_in_transit_candidate,
):
    producer_id, _product_ids, _open_qty = current_in_transit_candidate
    plan = policy.build_plan(producer_id, AS_OF, only_needed=False)
    with_oo = [it for it in plan.items if it.inventory.on_order > 0]
    assert with_oo, "expected current items carrying on_order"
    for it in with_oo:
        assert it.inventory.position > it.inventory.available - 1e-6
        assert abs(
            it.inventory.position
            - (it.inventory.available + it.inventory.on_order)
        ) < 1e-6


def test_producer_unit_costs_eur_are_sane_for_real_supplier(current_priced_candidate):
    _producer_id, _product_ids, costs = current_priced_candidate
    assert costs
    assert all(0 < c < 100000 for c in costs.values())


def test_build_plan_items_carry_eur_unit_cost(current_priced_candidate):
    producer_id, _product_ids, expected_costs = current_priced_candidate
    plan = policy.build_plan(producer_id, AS_OF, only_needed=False)
    priced = [it for it in plan.items if it.unit_cost_eur is not None]
    assert priced
    assert {it.product_id for it in priced}.issuperset(expected_costs)
    for it in priced:
        assert it.unit_cost_eur > 0
        expected_line_cost = (
            Decimal(str(it.unit_cost_eur)) * Decimal(str(it.suggested_qty))
        ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        assert Decimal(str(it.line_cost_eur)) == expected_line_cost


def test_supply_document_quantities_agree_with_the_derived_minimum(current_candidate):
    producer_id, _name, _product_ids = current_candidate
    minimums = {
        (t["producer_id"], t["product_id"]): t
        for t in repo.derive_moq_terms(AS_OF, 3)
        if t["producer_id"] == producer_id
    }
    documents = repo.supply_document_quantities(AS_OF, 3, producer_id=producer_id)
    assert set(documents) == set(minimums)
    for pair, found in documents.items():
        quantities = [qty for _day, _document, qty in found["documents"]]
        assert len(quantities) == minimums[pair]["orders"] >= 3
        assert min(quantities) == pytest.approx(minimums[pair]["moq"])
        assert found["pack"] == minimums[pair]["pack"]


def test_validated_derived_terms_carry_no_flag_and_reach_plan_lines(current_candidate):
    from app.services.replenishment import derived_terms

    producer_id, _name, product_ids = current_candidate
    found = derived_terms.derive(AS_OF, {producer_id: product_ids})
    validated = {t.product_id: t for t in found if t.validated}
    for term in validated.values():
        assert term.effective_moq <= term.sales_12m_units
        assert term.pack is None or abs(term.moq / term.pack - round(term.moq / term.pack)) < 1e-6
    plan = policy.build_plan(producer_id, AS_OF, only_needed=False, product_ids=product_ids)
    for item in plan.items:
        term = validated.get(item.product_id)
        if term is None:
            assert item.moq_basis is None and item.moq is None
        else:
            assert item.moq_basis == "derived_validated"
            assert item.moq == term.moq and item.pack_multiple == term.pack
            if item.suggested_qty > 0:
                assert item.suggested_qty >= term.effective_moq
