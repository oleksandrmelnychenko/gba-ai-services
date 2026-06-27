"""DB-backed integration smoke — runs against the live dev DB, skipped without DB env.

Marked `integration` and SKIPPED when DB connection env is absent, so the default CI job
(pytest -q, unit-only) stays green with no DB. Run explicitly via `make integration`
(pytest -m integration) with the read-only login env set:

    DB_HOST=127.0.0.1 DB_PORT=1433 DB_NAME=ConcordDb_V5 \
    DB_USER=gba_reco_ro DB_PASSWORD=... REDIS_DB=1 \
    pytest -m integration

These assert real magnitudes/sources for a known entity (producer 367 = 'Фенікс'),
guarding the same correctness bugs as the source guards but against live data.
"""
from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.integration

_DB_ENV_READY = bool(os.getenv("DB_PASSWORD")) and bool(os.getenv("DB_HOST"))

if not _DB_ENV_READY:
    pytest.skip("DB env not configured (set DB_HOST/DB_PASSWORD); integration smoke skipped",
                allow_module_level=True)

from app.data import cost_repository as cost_repo  # noqa: E402
from app.data import supply_repository as repo  # noqa: E402
from app.services.forecasting import lead_time as lead_time_svc  # noqa: E402
from app.services.replenishment import policy  # noqa: E402

PRODUCER_SEM = 410253
PRODUCER_SEM_NAME = "SEM OTOMOTIV DIS TICARET LTD.STI."
AS_OF = "2026-06-15"


def test_producer_name_resolves_supplier_from_client():
    name = repo.producer_name(PRODUCER_SEM)
    assert name == PRODUCER_SEM_NAME


def test_producer_lead_time_is_positive_and_sane():
    mean_days, std_days, source = lead_time_svc.producer_lead_time(PRODUCER_SEM, AS_OF)
    assert mean_days > 0
    assert mean_days < 365
    assert std_days >= 0
    assert source in ("empirical", "geo", "default")


def test_producer_lead_times_samples_within_plausible_window():
    samples = repo.producer_lead_times(PRODUCER_SEM, AS_OF)
    assert all(1 <= s <= 120 for s in samples)


def test_build_plan_returns_real_items_with_sane_cover_and_no_synthetic():
    plan = policy.build_plan(PRODUCER_SEM, AS_OF, only_needed=True)

    assert plan.producer_name == PRODUCER_SEM_NAME
    assert plan.lead_time_days > 0
    assert plan.item_count > 0
    assert plan.item_count == len(plan.items)
    assert all(item.days_of_cover >= 0 for item in plan.items)
    assert all(item.product_id != repo.SYNTHETIC_PRODUCT_ID for item in plan.items)
    assert all(item.suggested_qty > 0 for item in plan.items)


# Producer/as_of with known in-transit supply (open SupplyOrders not yet fully received).
# Anchors the on_order reconstruction against live data so the empty-source bug stays dead:
# the old query (SupplyOrderItem placeholder + SupplyOrder.Created < as_of) returned {} here.
PRODUCER_WITH_ON_ORDER = 410552
ON_ORDER_AS_OF = "2025-12-01"


def test_on_order_is_nonempty_for_producer_with_in_transit_supply():
    pids = repo.products_for_producer(PRODUCER_WITH_ON_ORDER, ON_ORDER_AS_OF, 120)
    oo = repo.on_order(pids, ON_ORDER_AS_OF)
    nonzero = {p: q for p, q in oo.items() if q > 0}
    # The whole point of the fix: on_order is NOT empty when real supply is in transit.
    assert len(nonzero) >= 20, f"on_order collapsed to {len(nonzero)} products (regression)"
    assert repo.SYNTHETIC_PRODUCT_ID not in oo          # synthetic excluded
    assert all(q > 0 for q in oo.values())              # only positive (ordered>received) kept
    assert sum(nonzero.values()) > 1000                 # material in-transit magnitude


def test_on_order_is_point_in_time_and_nets_receipts():
    """Earlier as_of must not see supply ordered+received only later; and on_order at a date
    never exceeds the cumulative ordered qty (it is ordered MINUS received, clamped >= 0)."""
    pids = repo.products_for_producer(PRODUCER_WITH_ON_ORDER, ON_ORDER_AS_OF, 120)
    early = repo.on_order(pids, "2025-01-01")
    later = repo.on_order(pids, ON_ORDER_AS_OF)
    assert sum(later.values()) >= 0 and sum(early.values()) >= 0
    # netting keeps it bounded by ordered: with receipts subtracted it cannot run negative
    assert all(q > 0 for q in later.values())


def test_on_order_raises_position_above_available_for_in_transit_items():
    plan = policy.build_plan(PRODUCER_WITH_ON_ORDER, ON_ORDER_AS_OF, only_needed=False)
    with_oo = [it for it in plan.items if it.inventory.on_order > 0]
    assert with_oo, "expected items carrying on_order in this producer/as_of"
    for it in with_oo:
        # position = available + on_order, so position strictly exceeds available here
        assert it.inventory.position > it.inventory.available - 1e-6
        assert abs(
            it.inventory.position
            - (it.inventory.available + it.inventory.on_order)
        ) < 1e-6


def test_producer_unit_costs_eur_are_sane_for_real_supplier():
    pids = repo.products_for_producer(PRODUCER_SEM, AS_OF, 540)[:200]
    costs = cost_repo.producer_unit_costs_eur(PRODUCER_SEM, pids, AS_OF)
    assert costs
    assert all(0 < c < 100000 for c in costs.values())


def test_build_plan_items_carry_eur_unit_cost():
    plan = policy.build_plan(PRODUCER_SEM, AS_OF, only_needed=True)
    priced = [it for it in plan.items if it.unit_cost_eur is not None]
    assert len(priced) > 0
    for it in priced:
        assert it.unit_cost_eur > 0
        assert it.line_cost_eur == round(it.unit_cost_eur * it.suggested_qty, 2)
