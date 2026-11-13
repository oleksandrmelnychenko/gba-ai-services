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


def test_in_progress_since_set_once(patched_mongo):
    from app.services import lifecycle
    k = lifecycle.upsert_generated(_task())
    assert patched_mongo["tasks"].find_one({"task_key": k}).get("in_progress_since") is None
    doc = lifecycle.change_status(k, TaskStatus.IN_PROGRESS, by=1)
    first = doc["in_progress_since"]
    assert first is not None
    # bounce back to open then in_progress again — the stamp records when work FIRST started.
    lifecycle.change_status(k, TaskStatus.OPEN, by=1)
    doc2 = lifecycle.change_status(k, TaskStatus.IN_PROGRESS, by=1)
    assert doc2["in_progress_since"] == first
    # may remain set on done (board/history can show duration).
    doc3 = lifecycle.change_status(k, TaskStatus.DONE, by=1)
    assert doc3["in_progress_since"] == first


def _team_task(mgr, client_id, key, urgency=Urgency.NORMAL, priority=50.0,
               ev_score: float | None = 0.0) -> Task:
    return Task(
        task_key=key, manager_id=mgr, client_id=client_id, client_name=f"Client {client_id}",
        task_type=TaskType.DEBT_FOLLOWUP, title="Call", reason="overdue",
        priority=priority, urgency=urgency, ev_score=ev_score,
        explanation=Explanation(factors=["x"], source_signal="debt", confidence=0.5),
        contact=Contact(phone="+380"),
    )


def test_inbox_ranks_equal_urgency_type_by_ev_score_and_sinks_unvalued_legacy(patched_mongo):
    from app.data import mongo
    from app.services import lifecycle

    high_ev = lifecycle.upsert_generated(
        _team_task(1, 20, "same_type_high_ev", Urgency.HIGH, priority=25.0, ev_score=800.0))
    high_probability = lifecycle.upsert_generated(
        _team_task(1, 21, "same_type_high_probability", Urgency.HIGH, priority=95.0, ev_score=50.0))
    legacy = lifecycle.upsert_generated(
        _team_task(1, 22, "same_type_legacy_priority", Urgency.HIGH, priority=90.0))
    mongo.tasks().update_one(
        {"task_key": legacy},
        {"$unset": {"p_outcome": "", "expected_value": "", "ev_score": ""}},
    )

    docs = lifecycle.inbox(1)
    keys = [d["task_key"] for d in docs]
    legacy_doc = next(d for d in docs if d["task_key"] == legacy)
    persisted = mongo.tasks().find_one({"task_key": legacy})

    assert keys == [high_ev, high_probability, legacy]
    assert legacy_doc["p_outcome"] == 0.9
    assert legacy_doc["expected_value"] == 0.0
    assert legacy_doc["ev_score"] == 0.0
    assert persisted["ev_score"] == 0.0


def test_inbox_backfills_legacy_debt_ev_fields_from_signals(patched_mongo):
    from app.data import mongo
    from app.services import lifecycle

    legacy = lifecycle.upsert_generated(
        _team_task(1, 23, "same_type_legacy_debt", Urgency.HIGH, priority=50.0))
    high_probability_low_ev = lifecycle.upsert_generated(
        _team_task(1, 24, "same_type_high_probability_low_ev", Urgency.HIGH,
                   priority=95.0, ev_score=100.0))
    mongo.tasks().update_one(
        {"task_key": legacy},
        {"$unset": {"p_outcome": "", "expected_value": "", "ev_score": ""},
         "$set": {"signals": {"overdue_amount": 1000.0}}},
    )

    docs = lifecycle.inbox(1)
    keys = [d["task_key"] for d in docs]
    legacy_doc = next(d for d in docs if d["task_key"] == legacy)
    persisted = mongo.tasks().find_one({"task_key": legacy})

    assert keys[:2] == [legacy, high_probability_low_ev]
    assert legacy_doc["p_outcome"] == 0.5
    assert legacy_doc["expected_value"] == 1000.0
    assert legacy_doc["ev_score"] == 500.0
    assert persisted["ev_score"] == 500.0


def test_team_tasks_ranks_equal_urgency_type_by_ev_score(patched_mongo, monkeypatch):
    from app.data import signals_repository
    from app.services import lifecycle

    monkeypatch.setattr(signals_repository, "manager_names", lambda ids: {i: f"Manager {i}" for i in ids})
    high_ev = lifecycle.upsert_generated(
        _team_task(1, 30, "team_high_ev", Urgency.HIGH, priority=20.0, ev_score=900.0))
    high_probability = lifecycle.upsert_generated(
        _team_task(1, 31, "team_high_probability", Urgency.HIGH, priority=95.0, ev_score=100.0))

    tasks, total, _ = lifecycle.team_tasks(["open"], manager_ids=[1])
    assert total == 2
    assert [t["task_key"] for t in tasks] == [high_ev, high_probability]


def test_team_tasks_paging_total_by_status_and_sort(patched_mongo, monkeypatch):
    from app.data import signals_repository
    from app.services import lifecycle
    monkeypatch.setattr(signals_repository, "manager_names",
                        lambda ids: {i: f"Manager {i}" for i in ids})

    # seed across managers + statuses + urgencies
    k_crit = lifecycle.upsert_generated(_team_task(1, 10, "m1c10", Urgency.CRITICAL, 50.0))
    k_hi_lo_pri = lifecycle.upsert_generated(_team_task(2, 11, "m2c11", Urgency.HIGH, 30.0))
    k_hi_hi_pri = lifecycle.upsert_generated(_team_task(1, 12, "m1c12", Urgency.HIGH, 90.0))
    k_ip = lifecycle.upsert_generated(_team_task(2, 13, "m2c13", Urgency.HIGH, 10.0))
    lifecycle.change_status(k_ip, TaskStatus.IN_PROGRESS, by=2)  # actively worked
    k_done = lifecycle.upsert_generated(_team_task(1, 14, "m1c14", Urgency.LOW, 5.0))
    lifecycle.change_status(k_done, TaskStatus.DONE, by=1)

    tasks, total, by_status = lifecycle.team_tasks(
        ["open", "in_progress"], manager_ids=None, urgency=None, skip=0, limit=50)
    keys = [t["task_key"] for t in tasks]
    # critical first; then high band — actively-worked (in_progress) ahead of equal-band opens,
    # then opens by priority desc; done excluded by the status filter.
    assert keys == [k_crit, k_ip, k_hi_hi_pri, k_hi_lo_pri]
    assert total == 4
    assert by_status == {"open": 3, "in_progress": 1, "done": 0, "snoozed": 0, "dismissed": 0}
    # joined name + denormalized stamp present on the in_progress row
    ip_row = next(t for t in tasks if t["task_key"] == k_ip)
    assert ip_row["manager_name"] == "Manager 2"
    assert ip_row["in_progress_since"] is not None

    # paging: page of 2 + total still over the whole filter
    page1, total1, _ = lifecycle.team_tasks(["open", "in_progress"], skip=0, limit=2)
    page2, total2, _ = lifecycle.team_tasks(["open", "in_progress"], skip=2, limit=2)
    assert [t["task_key"] for t in page1] == [k_crit, k_ip]
    assert [t["task_key"] for t in page2] == [k_hi_hi_pri, k_hi_lo_pri]
    assert total1 == total2 == 4

    # manager filter narrows to one manager but keeps the by_status rollup over that filter
    only1, total_only1, bs1 = lifecycle.team_tasks(["open", "in_progress", "done"], manager_ids=[1])
    assert {t["manager_id"] for t in only1} == {1}
    assert total_only1 == 3  # m1: 2 open + 1 done
    assert bs1 == {"open": 2, "in_progress": 0, "done": 1, "snoozed": 0, "dismissed": 0}

    # urgency filter
    crit_only, crit_total, _ = lifecycle.team_tasks(["open", "in_progress"], urgency="critical")
    assert [t["task_key"] for t in crit_only] == [k_crit] and crit_total == 1
    assert k_hi_lo_pri  # referenced
