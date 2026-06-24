"""State-machine + lifecycle tests using mongomock (no real Mongo needed)."""
from __future__ import annotations

import mongomock
import pytest

from app.domain.models import (
    ALLOWED_TRANSITIONS,
    Contact,
    Explanation,
    Task,
    TaskStatus,
    TaskType,
    Urgency,
)


@pytest.fixture
def patched_mongo(monkeypatch):
    client = mongomock.MongoClient()
    db = client["gba_nba_test"]
    from app.data import mongo as m
    monkeypatch.setattr(m, "get_client", lambda: client)
    monkeypatch.setattr(m, "get_db", lambda: db)
    monkeypatch.setattr(m, "tasks", lambda: db["tasks"])
    monkeypatch.setattr(m, "task_events", lambda: db["task_events"])
    monkeypatch.setattr(m, "manager_prefs", lambda: db["manager_prefs"])
    return db


def _task(key="mgr:1|client:10|type:debt_followup|win:2026-06") -> Task:
    return Task(
        task_key=key, manager_id=1, client_id=10, client_name="Acme",
        task_type=TaskType.DEBT_FOLLOWUP, title="Call", reason="overdue",
        priority=80.0, urgency=Urgency.HIGH,
        explanation=Explanation(factors=["overdue 12d"], source_signal="debt", confidence=0.8),
        contact=Contact(phone="+380"),
    )


def test_state_machine_terminal_states():
    assert ALLOWED_TRANSITIONS[TaskStatus.DONE] == set()
    assert ALLOWED_TRANSITIONS[TaskStatus.DISMISSED] == set()
    assert TaskStatus.DONE in ALLOWED_TRANSITIONS[TaskStatus.OPEN]


def test_upsert_then_inbox(patched_mongo):
    from app.services import lifecycle
    lifecycle.upsert_generated(_task())
    items = lifecycle.inbox(1)
    assert len(items) == 1
    assert items[0]["status"] == "open"
    assert items[0]["explanation"]["factors"] == ["overdue 12d"]
    assert items[0]["contact"]["phone"] == "+380"


def test_idempotent_generation_no_duplicate(patched_mongo):
    from app.services import lifecycle
    lifecycle.upsert_generated(_task())
    lifecycle.upsert_generated(_task())  # same key again
    assert len(lifecycle.inbox(1)) == 1


def test_mongo_generation_lock_is_exclusive(patched_mongo):
    from app.data import mongo
    mongo.ensure_indexes()

    assert mongo.acquire_lock("nba.generate.manager.1", "owner-a", 60) is True
    assert mongo.acquire_lock("nba.generate.manager.1", "owner-b", 60) is False

    mongo.release_lock("nba.generate.manager.1", "owner-a")
    assert mongo.acquire_lock("nba.generate.manager.1", "owner-b", 60) is True


def test_done_is_terminal_and_blocks_further(patched_mongo):
    from app.domain.models import Outcome
    from app.services import lifecycle
    k = lifecycle.upsert_generated(_task())
    lifecycle.change_status(k, TaskStatus.DONE, by=1, outcome=Outcome(sold=True, amount=5000))
    with pytest.raises(lifecycle.TransitionError):
        lifecycle.change_status(k, TaskStatus.OPEN, by=1)


def test_illegal_transition_rejected(patched_mongo):
    from app.services import lifecycle
    k = lifecycle.upsert_generated(_task())
    # generated->open happened on insert; open->done ok, but snoozed->done illegal path check:
    lifecycle.change_status(k, TaskStatus.SNOOZED, by=1)
    with pytest.raises(lifecycle.TransitionError):
        lifecycle.change_status(k, TaskStatus.IN_PROGRESS, by=1)  # snoozed can only ->open/dismissed


def test_dismiss_mutes_future(patched_mongo):
    from app.services import lifecycle
    k = lifecycle.upsert_generated(_task())
    lifecycle.change_status(k, TaskStatus.DISMISSED, by=1, reason="not relevant")
    assert lifecycle.is_muted(1, 10, "debt_followup") is True


def test_notes_append(patched_mongo):
    from app.services import lifecycle
    k = lifecycle.upsert_generated(_task())
    doc = lifecycle.add_note(k, author_id=1, text="call friday")
    assert doc["notes"][-1]["text"] == "call friday"


def test_done_task_not_regenerated(patched_mongo):
    from app.services import lifecycle
    k = lifecycle.upsert_generated(_task())
    lifecycle.change_status(k, TaskStatus.DONE, by=1)
    lifecycle.upsert_generated(_task())  # same key — should NOT reopen
    assert lifecycle.inbox(1) == []  # done is not in inbox; not resurrected
