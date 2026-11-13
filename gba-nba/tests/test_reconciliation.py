from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import mongomock
import pytest

from app.data import mongo, reconciliation_repository
from app.domain.models import Task, TaskStatus, TaskType
from app.services import lifecycle, orchestrator, reconciliation


@pytest.fixture
def db(monkeypatch):
    db = mongomock.MongoClient(tz_aware=True).nba
    db.tasks.create_index("task_key", unique=True)
    monkeypatch.setattr(mongo, "tasks", lambda: db.tasks)
    monkeypatch.setattr(mongo, "task_events", lambda: db.events)
    monkeypatch.setattr(mongo, "manager_prefs", lambda: db.preferences)
    return db


def task(key="old", kind=TaskType.REORDER_DUE, manager=1):
    return Task(task_key=key, client_id=10, manager_id=manager, task_type=kind, title="Call", reason="Due",
                payload={"products": [{"product_id": 7, "elapsed_days": 15}], "debt": {"debt_ids": [501]}},
                due_date=datetime.now(UTC) + timedelta(days=2))


def order(identifier=100, when="2026-10-01T12:00:00"):
    return {"order_id": identifier, "purchased_at": when}


def verified_order(identifier=100, when="2026-10-01T12:00:00"):
    return {**order(identifier, when), "timestamp_basis": "native_utc",
            "purchased_at_utc": when + "+00:00"}


def facts(monkeypatch, *, owner=1, debts=None, orders=None):
    value = {10: {"manager_id": owner, "debts": debts or {}, "orders": orders or {}}}
    monkeypatch.setattr(reconciliation_repository, "source_facts", lambda ids, asof: value)
    return value


def persist(candidate, asof="2026-10-16"):
    candidates, stats = reconciliation.prepare(candidate.manager_id, [candidate], asof)
    for item in candidates:
        lifecycle.upsert_generated(item)
    return candidates, stats


def test_paid_identified_debt_supersedes_with_evidence_and_keeps_notes_and_contact(db, monkeypatch):
    source = facts(monkeypatch, debts={"501": "12.50"})
    rows, _ = persist(task(kind=TaskType.DEBT_FOLLOWUP))
    key = rows[0].task_key
    lifecycle.add_note(key, 1, "Promise to call Friday")
    deadline = datetime.now(UTC) + timedelta(days=2)
    lifecycle.change_status(key, TaskStatus.SNOOZED, by=1, snooze_until=deadline)
    source[10]["debts"]["501"] = "0.00"
    candidates, stats = reconciliation.prepare(1, [], "2026-10-17")
    historical = db.tasks.find_one({"task_key": key})
    assert stats["superseded"] == 1 and candidates == []
    assert historical["status"] == "superseded"
    assert historical["source_evidence"]["debts"] == {"501": "0.00"}
    assert historical["notes"][0]["text"] == "Promise to call Friday"
    commitment = db.tasks.find_one({"source_task_key": key})
    assert commitment["task_type"] == "manual" and commitment["status"] == "snoozed"
    assert commitment["snooze_until"].date() == deadline.date()
    assert lifecycle.active_count(1, exclude_manual=True) == 0
    assert lifecycle.dashboard_counts(1, "2026-10-17")["manual_backlog"] == 1


@pytest.mark.parametrize("current", [{}, {"other": "0"}])
def test_missing_debt_is_not_paid_and_empty_generator_cannot_close(db, monkeypatch, current):
    source = facts(monkeypatch, debts={"501": "12.50"})
    rows, _ = persist(task(kind=TaskType.DEBT_FOLLOWUP))
    source[10]["debts"] = current
    _, stats = reconciliation.prepare(1, [], "2026-10-17")
    assert stats["superseded"] == 0
    assert db.tasks.find_one({"task_key": rows[0].task_key})["status"] == "open"


def test_purchase_supersedes_old_advice_and_second_cycle_in_same_month_is_distinct(db, monkeypatch):
    source = facts(monkeypatch, orders={"7": verified_order()})
    first, _ = persist(task())
    key = first[0].task_key
    source[10]["orders"]["7"] = verified_order(101, "2026-10-17T11:00:00")
    # Generator still reflects yesterday; don't create another call immediately after purchase.
    stale, stats = persist(task("monthly-old"), "2026-10-17")
    assert stale == [] and stats["superseded"] == 1
    second = task("monthly-old")
    second.payload["products"][0]["elapsed_days"] = 7
    rows, _ = persist(second, "2026-10-24")
    assert len(rows) == 1 and rows[0].task_key != key
    assert db.tasks.count_documents({"status": "open"}) == 1
    assert db.tasks.find_one({"task_key": key})["source_evidence"]["orders"]["7"]["order_id"] == 101


def test_returned_purchase_absence_does_not_close_existing_need(db, monkeypatch):
    source = facts(monkeypatch, orders={"7": order()})
    rows, _ = persist(task())
    # Fully returned orders are absent from retained purchase facts, not new purchase evidence.
    source[10]["orders"] = {}
    _, stats = reconciliation.prepare(1, [], "2026-10-17")
    assert stats["superseded"] == 0
    assert db.tasks.find_one({"task_key": rows[0].task_key})["status"] == "open"


def test_transfer_keeps_one_owner_identity_notes_and_history(db, monkeypatch):
    source = facts(monkeypatch, orders={"7": order()})
    first, _ = persist(task())
    lifecycle.add_note(first[0].task_key, 1, "Call agreed")
    source[10]["manager_id"] = 2
    rows, stats = persist(task("new-manager", manager=2))
    assert stats["reassigned"] == 1
    assert rows[0].task_key == first[0].task_key
    assert db.tasks.count_documents({"manager_id": 1}) == 0
    document = db.tasks.find_one({"manager_id": 2})
    assert document["assignment_history"][0]["from"] == 1
    assert document["notes"][0]["text"] == "Call agreed"


def test_failure_and_month_rollover_preserve_existing_identity(db, monkeypatch):
    facts(monkeypatch, orders={"7": order()})
    first, _ = persist(task())
    def unavailable(ids, asof):
        raise TimeoutError("partial source")
    monkeypatch.setattr(reconciliation_repository, "source_facts", unavailable)
    rows, stats = persist(task("next-month"), "2026-11-01")
    assert stats["reconciliation_failed"] == 1 and stats["superseded"] == 0
    assert rows[0].task_key == first[0].task_key
    assert db.tasks.count_documents({}) == 1


def test_same_terminal_cycle_stays_closed_across_month_rollover(db, monkeypatch):
    facts(monkeypatch, orders={"7": order()})
    first, _ = persist(task())
    lifecycle.change_status(first[0].task_key, TaskStatus.DONE, by=1)
    rows, _ = persist(task("November"), "2026-11-01")
    assert rows[0].task_key == first[0].task_key
    assert db.tasks.count_documents({"status": "open"}) == 0


def test_duplicate_legacy_monthly_advice_becomes_one_call(db, monkeypatch):
    facts(monkeypatch, orders={"7": order()})
    lifecycle.upsert_generated(task("month1"))
    lifecycle.upsert_generated(task("month2"))
    _, stats = reconciliation.prepare(1, [], "2026-10-16")
    assert stats["superseded"] == 1
    assert db.tasks.count_documents({"status": "open"}) == 1
    evidence = db.tasks.find_one({"status": "superseded"})["source_evidence"]
    assert evidence["kind"] == "duplicate_recommendation"


def test_manual_commitments_do_not_consume_per_client_generation_quota(db, monkeypatch):
    facts(monkeypatch, debts={"501": "12"})
    for i in range(4):
        lifecycle.create_manual(task(f"manual-{i}", kind=TaskType.MANUAL), created_by=1)
    assert lifecycle.active_counts_by_client(1) == {}
    assert lifecycle.active_counts_by_type(1) == {}
    generator = SimpleNamespace(__name__="debt", generate=lambda *args: [task(kind=TaskType.DEBT_FOLLOWUP)])
    monkeypatch.setattr(orchestrator, "_GENERATORS", [generator])
    from app.services import targets
    monkeypatch.setattr(targets, "compute_target", lambda *args: {})
    result = orchestrator.generate_for_manager(1, "2026-10-16")
    assert result["persisted"] == 1
    assert lifecycle.dashboard_counts(1, "2026-10-16")["manual_backlog"] == 4


def test_generator_failure_does_not_mean_absence(db, monkeypatch):
    facts(monkeypatch, orders={"7": order()})
    rows, _ = persist(task())
    def fail(*args):
        raise RuntimeError("model offline")
    monkeypatch.setattr(orchestrator, "_GENERATORS", [SimpleNamespace(__name__="failed", generate=fail)])
    from app.services import targets
    monkeypatch.setattr(targets, "compute_target", lambda *args: {})
    result = orchestrator.generate_for_manager(1, "2026-10-17")
    assert result["generators_failed"] == 1 and result["superseded"] == 0
    assert db.tasks.find_one({"task_key": rows[0].task_key})["status"] == "open"


def test_full_return_after_purchase_allows_renewed_need_without_resurrecting_history(db, monkeypatch):
    source = facts(monkeypatch, orders={"7": verified_order()})
    first, _ = persist(task())
    source[10]["orders"]["7"] = verified_order(101, "2026-10-17T11:00:00")
    reconciliation.prepare(1, [], "2026-10-17")
    assert db.tasks.find_one({"task_key": first[0].task_key})["status"] == "superseded"
    # That purchase is returned completely: the retained-purchase frontier goes back,
    # but positive return evidence gives the renewed need a new, stable identity.
    source[10]["orders"]["7"] = verified_order()
    source[10]["returns"] = {"7": {"order_id": 101, "quantity": "20",
                                      "returned_at": "2026-10-18T12:00:00"}}
    candidate = task("returned")
    candidate.payload["products"][0]["elapsed_days"] = 17
    rows, _ = persist(candidate, "2026-10-18")
    assert rows[0].task_key != first[0].task_key
    assert db.tasks.count_documents({"status": "open"}) == 1
    assert db.tasks.count_documents({"status": "superseded"}) == 1
    again, _ = persist(task("November"), "2026-11-01")
    assert again[0].task_key == rows[0].task_key


def test_concurrent_seller_update_prevents_stale_resolution_and_commitment_copy(db, monkeypatch):
    source = facts(monkeypatch, debts={"501": "12"})
    rows, _ = persist(task(kind=TaskType.DEBT_FOLLOWUP))
    key = rows[0].task_key
    lifecycle.change_status(key, TaskStatus.IN_PROGRESS, by=1)
    stale = db.tasks.find_one({"task_key": key})
    # A seller changes the task after reconciliation's read. Millisecond precision is explicit.
    db.tasks.update_one({"task_key": key}, {"$set": {"updated_at": datetime.now(UTC) + timedelta(seconds=1),
                         "notes": [{"text": "Updated after snapshot"}]}})
    source[10]["debts"]["501"] = "0"
    assert reconciliation._supersede(stale, {"kind": "debt_settled"}, datetime.now(UTC)) is False
    assert db.tasks.count_documents({"task_type": "manual"}) == 0
    assert db.tasks.find_one({"task_key": key})["notes"][0]["text"] == "Updated after snapshot"


@pytest.mark.parametrize("status", [TaskStatus.IN_PROGRESS, TaskStatus.SNOOZED])
def test_interrupted_commitment_copy_repairs_on_next_run(db, monkeypatch, status):
    source = facts(monkeypatch, debts={"501": "12"})
    rows, _ = persist(task(kind=TaskType.DEBT_FOLLOWUP))
    key = rows[0].task_key
    deadline = datetime.now(UTC) + timedelta(days=2)
    lifecycle.add_note(key, 1, "Existing agreed follow-up")
    lifecycle.change_status(key, status, by=1, snooze_until=deadline)
    before = db.tasks.find_one({"task_key": key})
    source[10]["debts"]["501"] = "0"
    original = reconciliation._preserve_commitment
    def interrupted(*args):
        raise RuntimeError("process stopped after status CAS")
    monkeypatch.setattr(reconciliation, "_preserve_commitment", interrupted)
    with pytest.raises(RuntimeError):
        reconciliation.prepare(1, [], "2026-10-17")
    assert db.tasks.find_one({"task_key": key})["status"] == "superseded"
    monkeypatch.setattr(reconciliation, "_preserve_commitment", original)
    reconciliation.prepare(1, [], "2026-10-17")
    assert db.tasks.count_documents({"source_task_key": key}) == 1
    assert "commitment_reconciliation_pending" not in db.tasks.find_one({"task_key": key})

    copied = db.tasks.find_one({"source_task_key": key})
    assert copied["status"] == status.value and copied["task_type"] == "manual"
    assert copied["notes"] == before["notes"] and copied["status_history"] == before["status_history"]
    assert copied["snooze_until"] == before["snooze_until"]
    assert copied["expires_at"] is None
    assert all(field not in copied for field in (
        "resolved_at", "source_evidence", "commitment_reconciliation_pending"))
    assert db.tasks.find_one({"task_key": key})["source_evidence"]["kind"] == "debt_settled"
    reconciliation.prepare(1, [], "2026-10-17")
    assert db.tasks.count_documents({"source_task_key": key}) == 1


def test_legacy_purchase_dates_are_conservative_and_public_evidence_is_date_only(db, monkeypatch):
    facts(monkeypatch, orders={"7": order(101, "2026-10-22T23:59:00")})
    lifecycle.upsert_generated(task("legacy"))
    # UTC generation on previous date can already be the same Kyiv business day.
    db.tasks.update_one({"task_key": "legacy"}, {"$set": {
        "generated_at": datetime(2026, 10, 21, 22, tzinfo=UTC)}})
    _, stats = reconciliation.prepare(1, [], "2026-10-22")
    assert stats["superseded"] == 0
    facts(monkeypatch, orders={"7": order(102, "2026-10-23T12:00:00")})
    _, stats = reconciliation.prepare(1, [], "2026-10-23")
    evidence = db.tasks.find_one({"task_key": "legacy"})["source_evidence"]["orders"]["7"]
    assert evidence == {"order_id": 102, "purchased_on": "2026-10-23", "date_basis": "source_document_date"}


def test_unrelated_debt_does_not_block_settlement_of_advised_documents(db, monkeypatch):
    source = facts(monkeypatch, debts={"501": "50", "502": "200"})
    rows, _ = persist(task(kind=TaskType.DEBT_FOLLOWUP))
    assert rows[0].source_baseline == {"debts": {"501": "50"}}
    source[10]["debts"]["501"] = "0"
    _, stats = reconciliation.prepare(1, [], "2026-10-17")
    assert stats["superseded"] == 1
    assert db.tasks.find_one({"task_key": rows[0].task_key})["source_evidence"]["debts"] == {"501": "0"}


def test_legacy_debt_without_identified_documents_stays_unknown(db, monkeypatch):
    source = facts(monkeypatch, debts={"501": "50"})
    legacy = task(kind=TaskType.DEBT_FOLLOWUP)
    legacy.payload.pop("debt")
    rows, _ = persist(legacy)
    source[10]["debts"]["501"] = "0"
    _, stats = reconciliation.prepare(1, [], "2026-10-17")
    assert stats["superseded"] == 0
    assert db.tasks.find_one({"task_key": rows[0].task_key})["status"] == "open"


def test_document_date_correction_is_not_new_purchase():
    assert not reconciliation._newer(order(100, "2026-10-20T12:00:00"), order(100), None)


def test_mixed_source_same_day_times_cannot_prove_later_purchase():
    # Imported 12:00 Kyiv actually precedes native 10:00 UTC, despite lexical ordering.
    assert not reconciliation._newer(order(101, "2026-10-01T12:00:00"), order(100, "2026-10-01T10:00:00"), None)


def test_reassignment_does_not_erase_concurrent_commitment_before_resolution(db, monkeypatch):
    source = facts(monkeypatch, debts={"501": "50"})
    rows, _ = persist(task(kind=TaskType.DEBT_FOLLOWUP))
    key = rows[0].task_key
    lifecycle.change_status(key, TaskStatus.IN_PROGRESS, by=1)
    source[10]["manager_id"] = 2
    source[10]["debts"]["501"] = "0"
    original = db.tasks.update_one
    deadline = datetime.now(UTC) + timedelta(days=10)
    def racing_update(query, update, *args, **kwargs):
        if "assignment_history" in update.get("$push", {}):
            original({"task_key": key}, {"$set": {"due_date": deadline, "updated_at": datetime.now(UTC) + timedelta(seconds=2)}})
        return original(query, update, *args, **kwargs)
    monkeypatch.setattr(db.tasks, "update_one", racing_update)
    _, stats = reconciliation.prepare(1, [], "2026-10-17")
    assert stats["superseded"] == 0 and stats["reassigned"] == 0
    assert db.tasks.find_one({"task_key": key})["due_date"].date() == deadline.date()
    assert db.tasks.count_documents({"origin": "seller_commitment"}) == 0


def test_refreshed_debt_advice_tracks_new_document_in_same_active_task(db, monkeypatch):
    source = facts(monkeypatch, debts={"501": "50", "502": "200"})
    first, _ = persist(task(kind=TaskType.DEBT_FOLLOWUP))
    expanded = task(kind=TaskType.DEBT_FOLLOWUP)
    expanded.payload["debt"]["debt_ids"] = [501, 502]
    second, _ = persist(expanded)
    assert first[0].task_key == second[0].task_key
    source[10]["debts"]["501"] = "0"
    _, stats = reconciliation.prepare(1, [], "2026-10-17")
    assert stats["superseded"] == 0
    source[10]["debts"]["502"] = "0"
    _, stats = reconciliation.prepare(1, [], "2026-10-17")
    assert stats["superseded"] == 1


def test_refresh_adds_baseline_for_new_product_without_retroactive_resolution(db, monkeypatch):
    facts(monkeypatch, orders={"7": order(), "8": order(200, "2026-10-15T12:00:00")})
    rows, _ = persist(task())
    key = rows[0].task_key
    db.tasks.update_one({"task_key": key}, {"$set": {"generated_at": datetime(2026, 10, 2, tzinfo=UTC)}})
    fresh = task()
    fresh.payload["products"] = [{"product_id": 8, "elapsed_days": 1}]
    persist(fresh)
    _, stats = reconciliation.prepare(1, [], "2026-10-17")
    assert stats["superseded"] == 0
    assert db.tasks.find_one({"task_key": key})["source_baseline"]["orders"]["8"]["order_id"] == 200


def test_stale_generator_cannot_reassign_transferred_task_back(db, monkeypatch):
    source = facts(monkeypatch, orders={"7": order()})
    old, _ = persist(task())
    stale_candidate = old[0]
    source[10]["manager_id"] = 2
    reconciliation.prepare(1, [], "2026-10-17")
    db.tasks.update_one({"task_key": stale_candidate.task_key}, {"$unset": {"due_date": ""}})
    events_before = db.events.count_documents({})
    lifecycle.upsert_generated(stale_candidate)
    actual = db.tasks.find_one({"task_key": stale_candidate.task_key})
    assert actual["manager_id"] == 2
    assert actual.get("due_date") is None
    assert db.events.count_documents({}) == events_before


def test_debt_generator_preserves_exact_document_ids(monkeypatch):
    from app.services.generators import debt_followup
    monkeypatch.setattr(debt_followup.sig, "debt_task_candidates", lambda *a, **kw: [{
        "client_id": 10, "debt_ids": "501,503", "max_days_past_terms": 2,
        "max_overdue_days": 12, "overdue_amount": 70, "debt_lines": 2}])
    monkeypatch.setattr(debt_followup.sig, "contacts_for_clients", lambda ids: {})
    monkeypatch.setattr(debt_followup.sig, "client_features", lambda ids, asof: {})
    generated = debt_followup.generate(1, "2026-10-17", "2026-10")
    assert generated[0].payload["debt"]["debt_ids"] == [501, 503]


def test_same_day_purchase_requires_verified_clock_and_distinct_document():
    prior = verified_order(100, "2026-10-22T09:00:00")
    actual = verified_order(101, "2026-10-22T10:00:00")
    assert reconciliation._newer(actual, prior, datetime(2026, 10, 22, 9, 30, tzinfo=UTC))
    assert not reconciliation._newer(verified_order(100, "2026-10-22T10:00:00"), prior, None)
    unknown = order(102, "2026-10-22T23:59:00")
    assert not reconciliation._newer(unknown, prior, datetime(2026, 10, 22, 9, 30, tzinfo=UTC))


def test_verified_same_day_purchase_can_resolve_legacy_baseline_after_generation():
    actual = verified_order(101, "2026-10-22T10:00:00")
    assert reconciliation._newer(actual, order(100), datetime(2026, 10, 22, 9, tzinfo=UTC))
    assert not reconciliation._newer(actual, order(100), datetime(2026, 10, 22, 11, tzinfo=UTC))


def test_verified_purchase_day_uses_kyiv_calendar():
    assert str(reconciliation._purchase_day(verified_order(100, "2026-10-21T22:00:00"))) == '2026-10-22'


def test_awakened_scheduled_followup_survives_source_resolution_as_open_manual(db, monkeypatch):
    source = facts(monkeypatch, debts={"501": "50"})
    rows, _ = persist(task(kind=TaskType.DEBT_FOLLOWUP))
    key = rows[0].task_key
    lifecycle.change_status(key, TaskStatus.SNOOZED, by=1,
                            snooze_until=datetime.now(UTC) + timedelta(days=1))
    db.tasks.update_one({'task_key': key}, {'$set': {'snooze_until': datetime.now(UTC) - timedelta(days=1)}})
    lifecycle.wake_snoozed()
    assert db.tasks.find_one({'task_key': key})['status'] == 'open'
    source[10]['debts']['501'] = '0'
    reconciliation.prepare(1, [], '2026-10-22')
    followup = db.tasks.find_one({'source_task_key': key})
    assert followup['task_type'] == 'manual' and followup['status'] == 'open'
    assert followup['expires_at'] is None


def test_head_task_assignment_survives_client_owner_reconciliation(db, monkeypatch):
    facts(monkeypatch, owner=1, orders={"7": order()})
    lifecycle.create_manual(task("delegated", kind=TaskType.MANUAL, manager=2), created_by=9)
    db.tasks.update_one({"task_key": "delegated"}, {"$set": {"assignment_mode": "head_task_only"}})
    _, stats = reconciliation.prepare(2, [], "2026-10-22")
    assert stats["reassigned"] == 0
    assert db.tasks.find_one({"task_key": "delegated"})["manager_id"] == 2
