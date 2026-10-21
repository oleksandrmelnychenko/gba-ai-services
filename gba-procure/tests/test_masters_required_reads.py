"""Required buyer input failures cannot become receipt-certified procurement plans."""

import pytest
from test_purchase_cost_cohort import plan_input as _plan_input

from app.data import masters
from app.services import reconciliation
from app.services.replenishment import policy, worker

AS_OF = "2026-09-08"
_real_profile = masters.producer_profile
_real_terms = masters.product_terms_for


class EmptyCollection:
    def find_one(self, *args, **kwargs):
        return None

    def find(self, *args, **kwargs):
        return iter(())


class DeniedCollection(EmptyCollection):
    def find_one(self, *args, **kwargs):
        raise RuntimeError("synthetic private backend detail")

    def find(self, *args, **kwargs):
        raise RuntimeError("synthetic private backend detail")


@pytest.fixture
def input_state(monkeypatch):
    cohort, products = _plan_input.__wrapped__(monkeypatch)
    # Exercise the real required readers, not the plan fixture's successful overrides.
    monkeypatch.setattr(masters, "producer_profile", _real_profile)
    monkeypatch.setattr(masters, "product_terms_for", _real_terms)
    monkeypatch.setattr(masters, "_producer_profiles", EmptyCollection)
    monkeypatch.setattr(masters, "_product_terms", EmptyCollection)
    monkeypatch.setattr(masters, "ping", lambda: True)
    settings = policy.get_settings().model_copy(update={"use_masters": True, "use_feedback": False})
    for module in (policy, worker, reconciliation):
        monkeypatch.setattr(module, "get_settings", lambda: settings)
    monkeypatch.setattr(
        policy.repo,
        "procurement_source_readiness",
        lambda *args: {"ready": True, "source_fingerprint": "fixture", "producer_count": 1},
    )
    monkeypatch.setattr(worker, "warm_producer_candidates", lambda *args: [7])
    monkeypatch.setattr(policy.repo, "products_for_producer", lambda *args: products[7])
    monkeypatch.setattr(worker, "read_purchase_cost_cohort", lambda *args: cohort)
    writes, markers = [], []
    monkeypatch.setattr(worker.cache, "get", lambda *args: None)
    monkeypatch.setattr(worker.cache, "set", lambda key, value, **kwargs: writes.append((key, value)) or True)
    monkeypatch.setattr(worker.cache, "delete", lambda *args: True)
    monkeypatch.setattr(worker.cache, "mark_cart_not_ready", lambda *a, **k: markers.append(k))
    return cohort, products, writes, markers


@pytest.mark.parametrize("collection", ["_producer_profiles", "_product_terms"])
def test_ping_ok_does_not_certify_denied_collection(monkeypatch, input_state, collection):
    monkeypatch.setattr(masters, collection, DeniedCollection)
    snapshot = worker.get_source_readiness(AS_OF, force=True)
    assert snapshot["masters_connected"] is True
    assert snapshot["masters_readable"] is False
    assert snapshot["ready"] is False
    assert snapshot["reason"] == "masters_collections_unavailable"
    with pytest.raises(worker.ProcurementBusinessReadinessError, match="masters_collections_unavailable"):
        worker.run(AS_OF)
    assert all("items" not in value for _, value in input_state[2])


def test_empty_collections_are_successful_required_reads(monkeypatch, input_state):
    cohort, products, _, _ = input_state
    assert worker.get_source_readiness(AS_OF, force=True)["ready"] is True
    assert masters.producer_profile(7, required=True) is None
    assert masters.product_terms_for(7, products[7], required=True) == {}
    plan = policy.build_plan(7, AS_OF, cost_cohort=cohort, product_ids=products[7])
    row = next(row for row in plan.items if row.product_id == 4)
    assert row.unit_cost_eur == 10 and row.cost_provenance.budget_eligible is True


@pytest.mark.parametrize("field", ["profile", "terms", "terms_cursor"])
def test_late_required_read_failure_never_publishes_a_producer_or_cart(monkeypatch, input_state, field):
    cohort, products, writes, markers = input_state

    class LateFailure(EmptyCollection):
        def find_one(self, query, *args, **kwargs):
            if query and field == "profile":
                raise RuntimeError("synthetic private backend detail")
            return None  # The bounded readiness probe genuinely succeeds.

        def find(self, *args, **kwargs):
            if field == "terms":
                raise RuntimeError("synthetic private backend detail")

            def broken_cursor():
                yield {"product_id": 4, "unit_cost_override": 1}
                raise RuntimeError("synthetic cursor interrupted after an override")

            return broken_cursor()

    monkeypatch.setattr(
        masters, "_producer_profiles" if field == "profile" else "_product_terms", LateFailure
    )
    assert worker.get_source_readiness(AS_OF, force=True)["ready"] is True
    expected = "masters_profile_read_failed" if field == "profile" else "masters_terms_read_failed"
    with pytest.raises(masters.MastersReadError, match=expected):
        policy.build_plan(7, AS_OF, cost_cohort=cohort, product_ids=products[7])
    with pytest.raises(worker.ProcurementBusinessReadinessError, match=expected):
        worker.run(AS_OF, warm_cart_key=False)
    with pytest.raises(worker.ProcurementBusinessReadinessError, match=expected):
        worker.warm_cart(AS_OF, cost_cohort=cohort, products_by_producer=products)
    assert all("items" not in value for _, value in writes)
    assert len(markers) == 2


def test_optional_absent_configuration_and_disabled_masters_remain_distinct(monkeypatch, input_state):
    cohort, products, _, _ = input_state
    monkeypatch.setattr(masters, "_producer_profiles", lambda: None)
    monkeypatch.setattr(masters, "_product_terms", lambda: None)
    assert masters.producer_profile(7) is None
    assert masters.product_terms_for(7, products[7]) == {}
    with pytest.raises(masters.MastersReadError, match="masters_store_unavailable"):
        masters.producer_profile(7, required=True)
    with pytest.raises(masters.MastersReadError, match="masters_store_unavailable"):
        masters.product_terms_for(7, products[7], required=True)
    disabled = policy.get_settings().model_copy(update={"use_masters": False, "use_feedback": False})
    monkeypatch.setattr(policy, "get_settings", lambda: disabled)
    monkeypatch.setattr(worker, "get_settings", lambda: disabled)
    assert worker.get_source_readiness(AS_OF, force=True)["ready"] is True
    assert policy.build_plan(7, AS_OF, cost_cohort=cohort, product_ids=products[7]).items


@pytest.mark.parametrize("phase", ["build_profile", "build_terms", "independent_facts"])
def test_reconciliation_required_read_failure_is_source_not_ready(monkeypatch, input_state, phase):
    cohort, products, _, _ = input_state
    good_plan = policy.build_plan(7, AS_OF, cost_cohort=cohort, product_ids=products[7])
    monkeypatch.setattr(
        masters, "_producer_profiles" if phase == "build_profile" else "_product_terms", DeniedCollection
    )
    if phase == "independent_facts":

        def factory():
            return good_plan

        def facts_loader(*args):
            reconciliation._collect_unit_cost_overrides({(7, 4)})
            pytest.fail("failed independent terms read became empty overrides")
    else:

        def factory():
            return policy.build_plan(7, AS_OF, cost_cohort=cohort, product_ids=products[7])

        def facts_loader(*args):
            pytest.fail("invalid plan reached independent fact comparison")

    result = reconciliation.run_reconciliation(
        AS_OF,
        120,
        factory,
        readiness_loader=lambda *a: {"ready": True},
        epoch_loader=lambda *a: "same",
        facts_loader=facts_loader,
    )
    assert result.exit_code == reconciliation.ReconciliationExitCode.SOURCE_NOT_READY
    assert [issue.code for issue in result.issues] == ["S003"]
    assert "buyer-maintained" in result.issues[0].message
    assert "synthetic private" not in str(result.to_dict())


def test_required_collection_resolution_error_is_safe(monkeypatch):
    def fail():
        raise RuntimeError("synthetic connection details")

    monkeypatch.setattr(masters, "_producer_profiles", fail)
    with pytest.raises(masters.MastersReadError, match="^masters_profile_read_failed$"):
        masters.producer_profile(7, required=True)
