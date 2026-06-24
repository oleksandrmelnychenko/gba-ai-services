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
