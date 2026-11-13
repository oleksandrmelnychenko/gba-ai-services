"""Stale-queue retirement: SLA-breached, untouched AI tasks leave the active queue."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import mongomock
import pytest

from app.domain.models import Contact, Explanation, Task, TaskStatus, TaskType, Urgency
from app.services import lifecycle


@pytest.fixture
def db(monkeypatch):
    client = mongomock.MongoClient()
    database = client["gba_nba_test"]
    from app.data import mongo as m
    monkeypatch.setattr(m, "get_client", lambda: client)
    monkeypatch.setattr(m, "get_db", lambda: database)
    monkeypatch.setattr(m, "tasks", lambda: database["tasks"])
    monkeypatch.setattr(m, "task_events", lambda: database["task_events"])
    monkeypatch.setattr(m, "manager_prefs", lambda: database["manager_prefs"])
    monkeypatch.setattr(m, "seller_contact_events", lambda: database["seller_contact_events"])
    return database


def _seed(db, key, *, client_id=10, task_type=TaskType.REORDER_DUE, urgency=Urgency.HIGH,
          age_days=20, breached=True, ev_score=10.0):
    lifecycle.upsert_generated(Task(
        task_key=key, manager_id=1, client_id=client_id, client_name="Acme", task_type=task_type,
        title="t", reason="r", priority=50.0, p_outcome=0.5, expected_value=ev_score * 2,
        ev_score=ev_score, urgency=urgency, explanation=Explanation(), contact=Contact(),
        due_date=datetime.now(UTC) - timedelta(days=1),
    ))
    created = datetime.now(UTC) - timedelta(days=age_days)
    db["tasks"].update_one({"task_key": key}, {"$set": {
        "generated_at": created, "sla_breached": breached,
        "status_history": [{"from": "generated", "to": "open", "at": created, "by": "system"}]}})
    return key


ALL_TYPES = {t.value for t in TaskType}


def test_breached_untouched_task_without_current_signal_is_retired_with_distinct_reason(db):
    gone = _seed(db, "gone", age_days=5)
    current = _seed(db, "current", client_id=11, age_days=5)
    stats = lifecycle.retire_stale(1, {current}, ALL_TYPES)
    assert stats == {"retired": 1, "by_type": {"reorder_due": 1},
                     "by_rule": {"signal_not_current": 1}}
    doc = db["tasks"].find_one({"task_key": gone})
    assert doc["status"] == "superseded"
    assert doc["retired_reason"] == "expired_stale"
    assert doc["retirement"]["rule"] == "signal_not_current"
    assert doc["resolved_at"] is not None
    assert doc["status_history"][-1]["reason"] == "expired_stale"
    assert doc["status_history"][-1]["by"] == "system"
    assert db["tasks"].find_one({"task_key": current})["status"] == "open"
    assert lifecycle._resolution_reason(doc["status_history"]) == "expired_stale"
    assert db["task_events"].find_one({"task_key": gone, "kind": "status:superseded"})["reason"] == (
        "expired_stale:signal_not_current")


def test_task_older_than_configured_age_is_retired_even_with_current_signal(db):
    old = _seed(db, "old", age_days=14)
    young = _seed(db, "young", client_id=11, age_days=13)
    stats = lifecycle.retire_stale(1, {old, young}, ALL_TYPES)
    assert stats["by_rule"] == {"max_age": 1}
    assert db["tasks"].find_one({"task_key": old})["status"] == "superseded"
    assert db["tasks"].find_one({"task_key": young})["status"] == "open"


def test_max_age_is_configurable(db, monkeypatch):
    from app.core.config import get_settings
    key = _seed(db, "k", age_days=5)
    monkeypatch.setattr(get_settings(), "stale_task_max_age_days", 3)
    assert lifecycle.retire_stale(1, {key}, ALL_TYPES)["retired"] == 1


def test_retirement_can_be_disabled(db, monkeypatch):
    from app.core.config import get_settings
    _seed(db, "k")
    monkeypatch.setattr(get_settings(), "stale_retirement_enabled", False)
    assert lifecycle.retire_stale(1, set(), ALL_TYPES)["retired"] == 0


def test_unbreached_task_is_never_retired(db):
    key = _seed(db, "fresh", age_days=40, breached=False)
    assert lifecycle.retire_stale(1, set(), ALL_TYPES)["retired"] == 0
    assert db["tasks"].find_one({"task_key": key})["status"] == "open"


def test_signal_rule_needs_an_evaluated_type(db):
    key = _seed(db, "cs", task_type=TaskType.CROSS_SELL, age_days=5)
    assert lifecycle.retire_stale(1, set(), {"reorder_due"})["retired"] == 0
    assert db["tasks"].find_one({"task_key": key})["status"] == "open"


def test_retirement_is_idempotent(db):
    _seed(db, "gone")
    first = lifecycle.retire_stale(1, set(), ALL_TYPES)
    events = db["task_events"].count_documents({})
    history = len(db["tasks"].find_one({"task_key": "gone"})["status_history"])
    second = lifecycle.retire_stale(1, set(), ALL_TYPES)
    assert first["retired"] == 1 and second["retired"] == 0
    assert db["task_events"].count_documents({}) == events
    assert len(db["tasks"].find_one({"task_key": "gone"})["status_history"]) == history


@pytest.mark.parametrize("touch", ["note", "in_progress", "snooze", "reopened", "contact",
                                   "manual", "attribution"])
def test_human_touched_tasks_are_never_retired(db, touch):
    key = _seed(db, "touched", age_days=60)
    if touch == "note":
        lifecycle.add_note(key, 1, "called, will pay")
    elif touch == "in_progress":
        lifecycle.change_status(key, TaskStatus.IN_PROGRESS, by=1)
    elif touch == "snooze":
        lifecycle.change_status(key, TaskStatus.SNOOZED, by=1,
                                snooze_until=datetime.now(UTC) - timedelta(hours=1))
        lifecycle.wake_snoozed()
    elif touch == "reopened":
        lifecycle.change_status(key, TaskStatus.IN_PROGRESS, by=1)
        lifecycle.change_status(key, TaskStatus.OPEN, by=1)
    elif touch == "contact":
        db["seller_contact_events"].insert_one(
            {"manager_id": 1, "client_id": 10, "at": datetime.now(UTC) - timedelta(days=2)})
    elif touch == "manual":
        db["tasks"].update_one({"task_key": key}, {"$set": {"task_type": "manual", "origin": "head"}})
    elif touch == "attribution":
        db["explicit_sale_attributions"].insert_one({"task_key": key, "manager_id": 1})
    before = db["tasks"].find_one({"task_key": key})["status"]
    assert lifecycle.retire_stale(1, set(), ALL_TYPES)["retired"] == 0
    assert db["tasks"].find_one({"task_key": key})["status"] == before


def test_contact_logged_before_generation_does_not_protect_the_task(db):
    key = _seed(db, "k", age_days=20)
    db["seller_contact_events"].insert_one(
        {"manager_id": 1, "client_id": 10, "at": datetime.now(UTC) - timedelta(days=30)})
    assert lifecycle.retire_stale(1, {key}, ALL_TYPES)["retired"] == 1


def test_current_critical_debt_keeps_reserved_slots_and_is_refreshed_not_duplicated(db, monkeypatch):
    from app.core.config import get_settings
    monkeypatch.setattr(get_settings(), "crit_debt_reserve", 2)
    keys = [_seed(db, f"debt{i}", client_id=20 + i, task_type=TaskType.DEBT_FOLLOWUP,
                  urgency=Urgency.CRITICAL, age_days=30, ev_score=100.0 - i) for i in range(3)]
    gone = _seed(db, "debt-gone", client_id=30, task_type=TaskType.DEBT_FOLLOWUP,
                 urgency=Urgency.CRITICAL, age_days=30, ev_score=500.0)
    stats = lifecycle.retire_stale(1, set(keys), ALL_TYPES)
    statuses = {key: db["tasks"].find_one({"task_key": key})["status"] for key in [*keys, gone]}
    assert statuses == {"debt0": "open", "debt1": "open", "debt2": "superseded",
                        "debt-gone": "superseded"}
    assert stats["by_rule"] == {"signal_not_current": 1, "max_age": 1}
    lifecycle.upsert_generated(Task(
        task_key="debt0", manager_id=1, client_id=20, task_type=TaskType.DEBT_FOLLOWUP,
        title="t", reason="refreshed", priority=60.0, urgency=Urgency.CRITICAL))
    assert db["tasks"].count_documents({"client_id": 20}) == 1
    assert db["tasks"].find_one({"task_key": "debt0"})["reason"] == "refreshed"


def test_retired_task_is_not_recreated_and_stays_out_of_active_counts_and_labels(db):
    key = _seed(db, "gone")
    lifecycle.retire_stale(1, set(), ALL_TYPES)
    lifecycle.upsert_generated(Task(
        task_key=key, manager_id=1, client_id=10, task_type=TaskType.REORDER_DUE,
        title="t", reason="again", priority=60.0))
    assert db["tasks"].find_one({"task_key": key})["status"] == "superseded"
    assert lifecycle.active_count(1) == 0
    assert lifecycle.feedback_rejections(1) == {}
    from app.ml import dataset
    assert len(dataset.live_labels()) == 0
    from app.services import workday
    assert "superseded" in workday.HISTORY


def test_generation_retires_before_admission_so_fresh_candidates_get_the_slots(db, monkeypatch):
    from types import SimpleNamespace

    from app.core.config import get_settings
    from app.services import orchestrator, reconciliation
    monkeypatch.setattr(get_settings(), "max_active_tasks_per_manager", 2)
    monkeypatch.setattr(get_settings(), "crit_debt_reserve", 0)
    _seed(db, "stale-a", client_id=41)
    _seed(db, "stale-b", client_id=42)

    def fresh(manager_id, as_of, window):
        return [Task(task_key=f"fresh-{cid}", manager_id=1, client_id=cid,
                     task_type=TaskType.REORDER_DUE, title="t", reason="r", priority=50.0,
                     ev_score=5.0) for cid in (51, 52)]

    monkeypatch.setattr(orchestrator, "_GENERATORS",
                        [SimpleNamespace(__name__="fresh", TYPE=TaskType.REORDER_DUE, generate=fresh)])
    monkeypatch.setattr(reconciliation, "prepare", lambda mid, candidates, as_of: (candidates, {}))
    monkeypatch.setattr(orchestrator, "_apply_pace_boost", lambda candidates, pace: None)
    stats = orchestrator.generate_for_manager(1, "2026-11-12")
    assert stats["stale_retired"] == 2
    assert stats["stale_retired_by_type"] == {"reorder_due": 2}
    assert stats["persisted"] == 2 and stats["skipped_capped"] == 0
    again = orchestrator.generate_for_manager(1, "2026-11-12")
    assert again["stale_retired"] == 0 and again["persisted"] == 0


def test_unchanged_refresh_writes_no_event_but_still_extends_the_task(db):
    task = Task(task_key="same", manager_id=1, client_id=10, task_type=TaskType.REORDER_DUE,
                title="t", reason="r", priority=50.0, payload={"products": [{"product_id": 7}]})
    lifecycle.upsert_generated(task)
    first = db["tasks"].find_one({"task_key": "same"})
    lifecycle.upsert_generated(task)
    lifecycle.upsert_generated(task)
    kinds = [event["kind"] for event in db["task_events"].find({"task_key": "same"})]
    assert kinds == ["generated"]
    assert db["tasks"].find_one({"task_key": "same"})["expires_at"] >= first["expires_at"]
    task.reason = "changed"
    lifecycle.upsert_generated(task)
    kinds = [event["kind"] for event in db["task_events"].find({"task_key": "same"})]
    assert kinds == ["generated", "refresh"]
    assert db["tasks"].find_one({"task_key": "same"})["reason"] == "changed"


def test_retired_business_cycle_is_not_regenerated_under_a_new_key(db, monkeypatch):
    from app.services import reconciliation
    old = _seed(db, "mgr:1|client:10|type:reorder_due|win:2026-10")
    lifecycle.retire_stale(1, set(), ALL_TYPES)
    facts = {10: {"manager_id": 1, "debts": {}, "returns": {},
                  "orders": {"7": {"order_id": 1, "purchased_at": "2026-10-01T10:00:00"}}}}
    monkeypatch.setattr(reconciliation.reconciliation_repository, "source_facts",
                        lambda ids, as_of: facts)
    candidate = Task(task_key="mgr:1|client:10|type:reorder_due|win:2026-11", manager_id=1,
                     client_id=10, task_type=TaskType.REORDER_DUE, title="t", reason="r",
                     priority=50.0, payload={"products": [{"product_id": 7, "elapsed_days": 40}]})
    retained, _ = reconciliation.prepare(1, [candidate], "2026-11-12")
    assert [task.task_key for task in retained] == [old]
    lifecycle.upsert_generated(retained[0])
    assert db["tasks"].count_documents({}) == 1
    assert db["tasks"].find_one({"task_key": old})["status"] == "superseded"
