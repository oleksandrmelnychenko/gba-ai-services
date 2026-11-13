"""Regressions: refreshing/reaping suggestions must preserve a seller's commitments."""
from datetime import UTC, datetime, timedelta

import mongomock
import pytest

from app.data import mongo
from app.domain.models import Task, TaskStatus, TaskType
from app.services import lifecycle


@pytest.fixture
def task_store(monkeypatch):
    db = mongomock.MongoClient(tz_aware=True).db
    clock = [datetime(2026, 10, 22, 8, tzinfo=UTC)]
    monkeypatch.setattr(mongo, "tasks", lambda: db.tasks)
    monkeypatch.setattr(mongo, "task_events", lambda: db.events)
    monkeypatch.setattr(mongo, "manager_prefs", lambda: db.prefs)
    monkeypatch.setattr(lifecycle, "_now", lambda: clock[0])
    return db, clock


def suggestion(key, due_date=None, task_type=TaskType.REORDER_DUE):
    return Task(task_key=key, manager_id=1, client_id=1, task_type=task_type,
                title="Fixture", reason="Fixture", due_date=due_date)


def test_refresh_keeps_deadline_and_sla_can_escalate(task_store):
    db, clock = task_store
    deadline = clock[0] - timedelta(days=1)
    lifecycle.upsert_generated(suggestion("reorder", deadline))
    lifecycle.upsert_generated(suggestion("reorder", clock[0] + timedelta(days=6)))
    assert db.tasks.find_one({"task_key": "reorder"})["due_date"] == deadline
    assert lifecycle.sweep_sla()["flagged"] == 1
    # Refresh also preserves the already-raised SLA exception.
    lifecycle.upsert_generated(suggestion("reorder", clock[0] + timedelta(days=6)))
    assert db.tasks.find_one({"task_key": "reorder"})["sla_breached"] is True


def test_refresh_can_assign_missing_deadline(task_store):
    db, clock = task_store
    lifecycle.upsert_generated(suggestion("reorder"))
    deadline = clock[0] + timedelta(days=1)
    lifecycle.upsert_generated(suggestion("reorder", deadline))
    assert db.tasks.find_one({"task_key": "reorder"})["due_date"] == deadline


def test_reaper_keeps_in_progress_noted_and_snoozed_work(task_store):
    db, clock = task_store
    for key in ("untouched", "in-progress", "noted", "snoozed", "reopened", "woken"):
        lifecycle.upsert_generated(suggestion(key))
    lifecycle.change_status("in-progress", TaskStatus.IN_PROGRESS, by=1)
    lifecycle.add_note("noted", 1, "Customer promised to call")
    lifecycle.change_status("snoozed", TaskStatus.SNOOZED, by=1,
                            snooze_until=clock[0] + timedelta(days=40))
    lifecycle.change_status("reopened", TaskStatus.IN_PROGRESS, by=1)
    lifecycle.change_status("reopened", TaskStatus.OPEN, by=1)
    lifecycle.change_status("woken", TaskStatus.SNOOZED, by=1,
                            snooze_until=clock[0] + timedelta(days=1))
    db.tasks.update_many({}, {"$set": {"expires_at": clock[0] - timedelta(days=1)}})
    clock[0] += timedelta(days=2)
    assert lifecycle.wake_snoozed() == 1
    assert lifecycle.sweep_expired() == 1
    assert set(db.tasks.distinct("task_key")) == {
        "in-progress", "noted", "snoozed", "reopened", "woken",
    }


def test_snoozed_manual_assignment_never_gets_expiry(task_store):
    db, clock = task_store
    lifecycle.create_manual(suggestion("manual", task_type=TaskType.MANUAL), created_by=2)
    lifecycle.change_status("manual", TaskStatus.SNOOZED, by=1,
                            snooze_until=clock[0] + timedelta(days=1))
    assert db.tasks.find_one({"task_key": "manual"})["expires_at"] is None
    # Repair safety for old manual rows that already acquired an expiry in the prior code.
    db.tasks.update_one({"task_key": "manual"}, {"$set": {"expires_at": clock[0]}})
    clock[0] += timedelta(days=30)
    assert lifecycle.wake_snoozed() == 1
    assert lifecycle.sweep_expired() == 0
    assert db.tasks.count_documents({"task_key": "manual"}) == 1


@pytest.mark.parametrize("had_deadline", [False, True])
def test_interleaved_refresh_never_overwrites_a_concurrently_assigned_deadline(
    task_store, monkeypatch, had_deadline,
):
    db, clock = task_store
    initial = clock[0] + timedelta(days=1) if had_deadline else None
    lifecycle.upsert_generated(suggestion("reorder", initial))
    winning_deadline = clock[0] + timedelta(days=2)
    stale_refresh_deadline = clock[0] + timedelta(days=7)
    original_find_one = db.tasks.find_one
    interleaved = False

    def interleave_after_read(*args, **kwargs):
        nonlocal interleaved
        snapshot = original_find_one(*args, **kwargs)
        if not interleaved:
            interleaved = True
            # A second actor establishes/updates the commitment after B's stale read.
            db.tasks.update_one({"task_key": "reorder"}, {"$set": {"due_date": winning_deadline}})
        return snapshot

    monkeypatch.setattr(db.tasks, "find_one", interleave_after_read)
    lifecycle.upsert_generated(suggestion("reorder", stale_refresh_deadline))
    assert interleaved
    assert original_find_one({"task_key": "reorder"})["due_date"] == winning_deadline
