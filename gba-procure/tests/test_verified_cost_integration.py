"""Cross-layer regressions using synthetic immutable observations; no live stores."""

from copy import deepcopy
from decimal import Decimal
from threading import Event
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from test_purchase_cost_cohort import plan_input as cohort_plan_input

from app.api import main
from app.data.purchase_cost_metadata import stable_manifest
from app.services import reconciliation as recon
from app.services.replenishment import policy, worker
from app.services.replenishment.warm_requests import BoundedWarmQueue


@pytest.fixture
def captured(monkeypatch):
    cohort, products = cohort_plan_input.__wrapped__(monkeypatch)
    plan = policy.build_cart_plan("2026-09-08", cost_cohort=cohort, products_by_producer=products)
    return plan, cohort


def test_cache_gate_checks_canonical_scalar_and_coordinated_fake_totals(captured):
    plan, _ = captured
    original = plan.model_dump(mode="json")
    assert worker.canonical_cart_payload_is_ready(original)
    row = next(item for item in original["items"] if item["product_id"] == 2)
    row["unit_cost_eur"] += 20
    row["line_cost_eur"] += 20 * row["suggested_qty"]
    original["priced_cost_eur"] += 20 * row["suggested_qty"]
    assert not worker.plan_cost_metadata_is_ready(original)
    assert not worker.canonical_cart_payload_is_ready(original)


@pytest.mark.parametrize(
    "field,value",
    [("aggregation_reason", "mixed_current_units"), ("budget_exclusion_reasons", []), ("known_count", 0)],
)
def test_cache_gate_rejects_contradictory_partial_proof(captured, field, value):
    plan, _ = captured
    raw = plan.model_dump(mode="json")
    item = next(item for item in raw["items"] if item["product_id"] == 3)
    item["cost_provenance"][field] = value
    assert not worker.plan_cost_metadata_is_ready(raw)


def test_cache_gate_requires_semantic_fields_and_exact_component_scope(captured):
    plan, _ = captured
    for mutate in (
        lambda p: p["items"][0].pop("sale_price_basis"),
        lambda p: p["items"][0].update(budget_priority_weight=0.7),
        lambda p: p["cost_observation_manifest"]["components"][0].update(history_days=539),
    ):
        raw = plan.model_dump(mode="json")
        mutate(raw)
        assert not worker.plan_cost_metadata_is_ready(raw)


@pytest.mark.parametrize(
    "version,ready",
    [
        ("GBA-ONEC-INBOUND-V7", True),
        ("GBA-ONEC-INBOUND-V8", True),
        ("GBA-ONEC-INBOUND-V6", False),
        ("GBA-ONEC-INBOUND-V9", False),
    ],
)
def test_cache_gate_keeps_carts_proven_by_current_v8_receipts(captured, version, ready):
    plan, _ = captured
    raw = plan.model_dump(mode="json")
    publications = [
        publication
        for component in raw["cost_observation_manifest"]["components"]
        for publication in component["publications"]
    ]
    assert publications
    for publication in publications:
        publication["contract_version"] = version
    assert worker.plan_cost_metadata_is_ready(raw) is ready
    assert worker.canonical_cart_payload_is_ready(raw) is ready


def test_queue_singleflight_bounds_failure_cooldown_and_shared_lifetime():
    queue = BoundedWarmQueue(capacity=2, cooldown_seconds=30)
    started, release = Event(), Event()
    seen = []

    def first():
        started.set()
        assert release.wait(3)
        seen.append("first")

    try:
        assert queue.submit("same", first)
        assert started.wait(3)
        assert queue.submit("same", lambda: seen.append("duplicate"))
        assert queue.submit("second", lambda: seen.append("second"))
        assert not queue.submit("overflow", lambda: None)
        release.set()
    finally:
        release.set()
        queue.shutdown()
    assert seen == ["first", "second"]
    failed = BoundedWarmQueue(cooldown_seconds=30)
    done = Event()

    def fail():
        done.set()
        raise ValueError("synthetic failure")

    assert failed.submit("failed", fail)
    assert done.wait(3)
    failed.shutdown()
    assert not failed.submit("failed", lambda: pytest.fail("failure cooldown bypass"))


@pytest.fixture
def api_memory(monkeypatch):
    store, jobs = {}, {}
    monkeypatch.setattr(main, "_today", lambda: "2026-09-08")
    monkeypatch.setattr(
        worker, "require_source_readiness", lambda *a, **k: {"ready": True, "source_fingerprint": "same"}
    )
    monkeypatch.setattr(main.cache, "get", lambda key: deepcopy(store.get(key)))
    monkeypatch.setattr(main.cache, "set", lambda key, payload, **k: store.setdefault(key, deepcopy(payload)))
    monkeypatch.setattr(
        main, "REQUEST_WARMS", SimpleNamespace(submit=lambda key, work: jobs.setdefault(key, work))
    )
    return store, jobs


def test_cold_api_returns_not_ready_without_running_any_cost_build(api_memory, monkeypatch):
    _, jobs = api_memory
    monkeypatch.setattr(policy, "build_plan", lambda *a, **k: pytest.fail("request ran cold proof"))
    monkeypatch.setattr(policy, "build_cart_plan", lambda *a, **k: pytest.fail("request ran cold proof"))
    monkeypatch.setattr(policy, "build_charts", lambda *a, **k: pytest.fail("request ran cold proof"))
    requests = [
        (main.plan_producer, main.PlanRequest(producer_id=7)),
        (main.plan_cart, main.CartPlanRequest()),
        (main.plan_charts, main.PlanChartsRequest()),
        (main.plan_cart, main.CartPlanRequest(active_days=90, only_needed=False)),
    ]
    for func, request in requests * 2:
        with pytest.raises(HTTPException) as error:
            func(request)
        assert error.value.status_code == 503
    assert len(jobs) == 4


def test_budget_variant_retains_captured_cohort_and_never_requotes(captured, api_memory, monkeypatch):
    plan, _ = captured
    store, jobs = api_memory
    payload = plan.model_dump(mode="json")
    payload["_source_fingerprint"] = "same"
    store[worker.cart_cache_key("2026-09-08")] = payload
    monkeypatch.setattr(policy, "read_purchase_cost_cohort", lambda *a: pytest.fail("new proof for budget"))
    budget = main.plan_cart(main.CartPlanRequest(budget_eur=1))
    assert budget.cost_observation_manifest == plan.cost_observation_manifest
    assert budget.selected_count == 1 and budget.budget_score == 1
    assert not jobs
    unbounded = main.plan_cart(main.CartPlanRequest(budget_eur=0))
    assert unbounded.budget_eur is None and unbounded.items == plan.items


def source_facts(plan, cohort):
    observations = tuple(row for snapshot in cohort.snapshots for row in snapshot.observations)
    rows = {}
    for row in observations:
        if row.status == "known":
            rows.setdefault((row.supplier_id, row.product_id), []).append(row.unit_net_cost_eur)
    inventory = {
        item.product_id: recon.InventoryFact(
            Decimal(str(item.inventory.on_hand)),
            Decimal(str(item.inventory.reserved)),
            Decimal(str(item.inventory.available)),
            Decimal(str(item.inventory.on_order)),
        )
        for item in plan.items
    }
    return recon.SourceFacts(
        inventory,
        rows,
        {},
        {},
        {"products_with_reservations": 1, "products_with_on_order": 1},
        unit_cost_overrides_by_pair={(7, 4): Decimal("1.0")},
        cost_observations=observations,
    )


def test_reconciliation_recomputes_coverage_contracts_and_median_without_cohort_oracle(captured, monkeypatch):
    plan, cohort = captured
    facts = source_facts(plan, cohort)
    payload = plan.model_dump(mode="json")
    monkeypatch.setattr(type(cohort), "group", lambda *a: pytest.fail("self oracle"))
    issues, metrics = recon.validate_canonical_plan(payload, facts, "2026-09-08")
    assert not [issue for issue in issues if issue.severity == "error"]
    costs = metrics["purchase_costs"]
    assert costs["selected_pair_count"] == 5
    assert costs["complete_pair_count"] == 2 and costs["partial_pair_count"] == 1
    assert costs["unknown_pair_count"] == 1 and costs["buyer_supplied_pair_count"] == 1
    assert costs["budget_eligible_pair_count"] == 2 and costs["computed_unpriced_items"] == 1
    forged = deepcopy(payload)
    next(row for row in forged["items"] if row["product_id"] == 3)["cost_provenance"][
        "supplier_client_agreement_ids"
    ] = [999]
    errors, _ = recon.validate_canonical_plan(forged, facts, "2026-09-08")
    assert any(issue.code == "C020" for issue in errors)


def test_reconciliation_source_change_and_validation_timestamps(captured):
    plan, cohort = captured
    facts = source_facts(plan, cohort)
    first = plan.model_dump(mode="json")
    second = deepcopy(first)
    component = second["cost_observation_manifest"]["components"][0]
    component["observation_started_at_utc"] = "2026-09-08T09:00:00.0000001Z"
    component["observation_completed_at_utc"] = "2026-09-08T09:00:01.0000001Z"
    for publication in component["publications"]:
        publication["validated_at_utc"] = "2026-09-08T09:00:00.0000002Z"
    assert stable_manifest(first["cost_observation_manifest"]) == stable_manifest(
        second["cost_observation_manifest"]
    )
    plans = iter([first, second])
    result = recon.run_reconciliation(
        "2026-09-08",
        120,
        lambda: next(plans),
        readiness_loader=lambda *a: {"ready": True},
        epoch_loader=lambda *a: "same",
        facts_loader=lambda *a: facts,
    )
    assert result.ok and result.to_dict()["schema_version"] == 2
    facts.cost_manifest_matches = False
    result = recon.run_reconciliation(
        "2026-09-08",
        120,
        lambda: first,
        repeat_builds=1,
        readiness_loader=lambda *a: {"ready": True},
        epoch_loader=lambda *a: "same",
        facts_loader=lambda *a: facts,
    )
    assert result.exit_code == recon.ReconciliationExitCode.SOURCE_NOT_READY
    assert any(issue.code == "S004" for issue in result.issues)


def test_worker_limit_still_reads_one_full_cart_cohort_and_reuses_for_every_plan(captured, monkeypatch):
    plan, cohort = captured
    source = {"ready": True, "source_fingerprint": "same", "producer_count": 2}
    monkeypatch.setattr(worker, "require_source_readiness", lambda *a, **k: source)
    monkeypatch.setattr(worker, "warm_producer_candidates", lambda *a: [7, 8])
    monkeypatch.setattr(policy.repo, "all_producers", lambda *a: [7, 8])
    monkeypatch.setattr(
        policy.repo, "products_for_producer", lambda pid, *a: [1, 2, 3, 4] if pid == 7 else [5]
    )
    reads = []
    monkeypatch.setattr(
        worker, "read_purchase_cost_cohort", lambda products, as_of: reads.append(products) or cohort
    )
    original = policy.build_plan
    reused = []

    def build(*args, **kwargs):
        reused.append(kwargs["cost_cohort"])
        return original(*args, **kwargs)

    monkeypatch.setattr(policy, "build_plan", build)
    store = {}
    monkeypatch.setattr(worker.cache, "get", store.get)
    monkeypatch.setattr(worker.cache, "set", lambda key, value, **kwargs: store.setdefault(key, value))
    monkeypatch.setattr(worker.cache, "get_cart_not_ready", lambda *a: None)
    monkeypatch.setattr(worker.cache, "clear_cart_not_ready", lambda *a: True)
    result = worker.run("2026-09-08", limit=1, warm_cart_key=True)
    assert reads == [[1, 2, 3, 4, 5]]
    assert len(reused) == 3 and all(value is cohort for value in reused)
    assert result["failed"] == 0 and result["cart_items"] == 5
    assert result["cart"]["items"] == 5 and result["charts"]["top_items"] == 5
    cart = store[worker.cart_cache_key("2026-09-08")]
    charts = store[worker.cache.make_key("charts", "all:15", "2026-09-08")]
    assert cart["cost_observation_manifest"] == charts["cost_observation_manifest"]


@pytest.mark.parametrize("changed", [False, True])
def test_worker_resume_always_revalidates_proof_but_ignores_validation_time_only(
    captured, monkeypatch, changed
):
    from dataclasses import replace
    from datetime import date

    from app.data.purchase_cost_cohort import PurchaseCostCohort

    _, cohort = captured
    old = policy.build_plan(7, "2026-09-08", cost_cohort=cohort, product_ids=[1, 2, 3, 4, 5])
    snapshots = tuple(
        replace(
            snapshot,
            observation_started_at_utc="2026-09-08T10:00:00Z",
            observation_completed_at_utc="2026-09-08T10:00:02Z",
            fingerprint="d" * 64 if changed else snapshot.fingerprint,
            publications=tuple(
                replace(pub, validated_at_utc="2026-09-08T10:00:01Z") for pub in snapshot.publications
            ),
        )
        for snapshot in cohort.snapshots
    )
    fresh = PurchaseCostCohort(tuple(sorted(cohort.product_ids)), date(2026, 9, 8), snapshots)
    monkeypatch.setattr(
        worker, "require_source_readiness", lambda *a, **k: {"ready": True, "source_fingerprint": "same"}
    )
    monkeypatch.setattr(worker, "warm_producer_candidates", lambda *a: [7])
    monkeypatch.setattr(policy.repo, "products_for_producer", lambda *a: [1, 2, 3, 4, 5])
    reads = []
    monkeypatch.setattr(worker, "read_purchase_cost_cohort", lambda *a: reads.append(1) or fresh)
    payload = old.model_dump(mode="json")
    payload["_source_fingerprint"] = "same"
    monkeypatch.setattr(worker.cache, "get", lambda *a: payload)
    writes = []
    monkeypatch.setattr(worker.cache, "set", lambda *a, **k: writes.append(a[1]) or True)
    result = worker.run("2026-09-08", warm_cart_key=False, skip_existing=True)
    assert reads == [1]
    assert result["skipped"] == (0 if changed else 1)
    assert len(writes) == (1 if changed else 0)


def test_worker_required_transport_failure_cannot_publish_empty_cohort(monkeypatch):
    from app.data.native_purchase_costs import NativePurchaseCostError

    monkeypatch.setattr(worker, "require_source_readiness", lambda *a, **k: {"ready": True})
    monkeypatch.setattr(worker, "warm_producer_candidates", lambda *a: [7])
    monkeypatch.setattr(policy.repo, "products_for_producer", lambda *a: [1])

    def fail(*args):
        raise NativePurchaseCostError("purchase_cost_transport_failed")

    monkeypatch.setattr(worker, "read_purchase_cost_cohort", fail)
    monkeypatch.setattr(
        worker.cache, "set", lambda *a, **k: pytest.fail("transport failure became cached empty")
    )
    with pytest.raises(NativePurchaseCostError):
        worker.run("2026-09-08", warm_cart_key=False)


def test_cheaper_alt_requires_comparable_receipt_net_cost_with_exact_two_percent_boundary(captured):
    from native_cost_fixture import make_cohort

    from app.data.purchase_cost_cohort import buyer_provenance

    plan, _ = captured
    base = next(item for item in plan.items if item.product_id == 2)
    cohort = make_cohort([2], costs={(7, 2): ["100"], (8, 2): ["98"]})
    base.cost_provenance = cohort.provenance(2, 7, base.suggested_qty)
    policy._attach_cheaper_alt(7, [base], "2026-09-08", cohort)
    assert base.cheaper_alt.cost_eur == 98
    base.cheaper_alt = None
    base.cost_provenance = buyer_provenance(100, base.suggested_qty)
    policy._attach_cheaper_alt(7, [base], "2026-09-08", cohort)
    assert base.cheaper_alt is None
    base.unit_cost_eur = None
    base.cost_provenance = make_cohort([2], costs={(7, 2): [None]}).provenance(2, 7, base.suggested_qty)
    policy._attach_cheaper_alt(7, [base], "2026-09-08", cohort)
    assert base.cheaper_alt is None


def test_cache_rejects_buyer_override_disguised_as_historical_cheaper_alternative(captured):
    from app.data.purchase_cost_cohort import buyer_provenance

    plan, _ = captured
    payload = plan.model_dump(mode="json")
    item = next(row for row in payload["items"] if row["product_id"] == 2)
    item["cheaper_alt"] = {
        "producer_id": 8,
        "cost_eur": 1,
        "comparison_basis": "historical_net_goods",
        "cost_provenance": buyer_provenance(1).model_dump(mode="json"),
    }
    assert not worker.plan_cost_metadata_is_ready(payload)


def test_reconciliation_transport_failure_during_build_is_source_not_ready():
    from app.data.native_purchase_costs import NativePurchaseCostError

    def fail():
        raise NativePurchaseCostError("purchase_cost_transport_failed")

    result = recon.run_reconciliation(
        "2026-09-08", 120, fail, readiness_loader=lambda *a: {"ready": True}, epoch_loader=lambda *a: "same"
    )
    assert result.exit_code == recon.ReconciliationExitCode.SOURCE_NOT_READY
    assert [issue.code for issue in result.issues] == ["S003"]


def test_known_unknown_cost_remains_valid_when_stock_examples_are_required(captured):
    plan, cohort = captured
    issues, _ = recon.validate_canonical_plan(
        plan.model_dump(mode="json"), source_facts(plan, cohort), "2026-09-08", strict_coverage=True
    )
    assert not [issue for issue in issues if issue.severity == "error"]
    assert any(issue.code == "G003" and issue.severity == "warning" for issue in issues)


def test_cache_malformed_native_proof_is_not_ready_without_throwing(captured):
    plan, _ = captured
    payload = plan.model_dump(mode="json")
    payload["cost_observation_manifest"]["components"][0]["fingerprint"] = "invalid"
    assert not worker.plan_cost_metadata_is_ready(payload)
    payload = plan.model_dump(mode="json")
    payload["cost_totals_certified"] = 0
    assert not worker.plan_cost_metadata_is_ready(payload)


def test_reconciliation_does_not_treat_missing_scalar_as_confirmed_zero(captured):
    plan, cohort = captured
    payload = plan.model_dump(mode="json")
    row = next(item for item in payload["items"] if item["product_id"] == 1)
    assert row["unit_cost_eur"] == row["line_cost_eur"] == 0
    row["unit_cost_eur"] = row["line_cost_eur"] = None
    issues, _ = recon.validate_canonical_plan(payload, source_facts(plan, cohort), "2026-09-08")
    assert {"M002", "M003"}.issubset({issue.code for issue in issues})
