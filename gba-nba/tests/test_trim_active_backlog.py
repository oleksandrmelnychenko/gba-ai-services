from __future__ import annotations

import mongomock
import pytest

from app.domain.models import TaskStatus, TaskType, Urgency


@pytest.fixture
def mongo_db(monkeypatch):
    client = mongomock.MongoClient()
    db = client["gba_nba_test"]
    from app.data import mongo as m
    monkeypatch.setattr(m, "get_client", lambda: client)
    monkeypatch.setattr(m, "get_db", lambda: db)
    monkeypatch.setattr(m, "tasks", lambda: db["tasks"])
    monkeypatch.setattr(m, "task_events", lambda: db["task_events"])
    monkeypatch.setattr(m, "manager_prefs", lambda: db["manager_prefs"])
    return db


def _task(key: str, ev_score: float, status: TaskStatus = TaskStatus.OPEN):
    from app.domain.models import Task
    return Task(
        task_key=key,
        manager_id=1,
        client_id=abs(hash(key)) % 1_000_000,
        client_name=key,
        task_type=TaskType.DEBT_FOLLOWUP,
        title="t",
        reason="r",
        priority=50.0,
        ev_score=ev_score,
        urgency=Urgency.HIGH,
        status=status,
    )


def test_trim_plan_keeps_top_ranked_open_tasks(mongo_db):
    from app.services import lifecycle
    from scripts.trim_active_backlog import plan_manager_trim

    for i, ev in enumerate([100.0, 90.0, 80.0, 70.0, 60.0]):
        lifecycle.upsert_generated(_task(f"t{i}", ev))

    plan = plan_manager_trim(1, target_active=3)

    assert plan["active_before"] == 5
    assert plan["active_after"] == 3
    assert plan["trim_count"] == 2
    assert [d["task_key"] for d in mongo_db["tasks"].find({}, {"_id": 0, "task_key": 1})] == [
        "t0", "t1", "t2", "t3", "t4"
    ]
    assert [item["task_key"] for item in plan["trim_boundary"]] == ["t3", "t4"]


def test_apply_plan_deletes_tail_and_writes_audit_event(mongo_db):
    from app.services import lifecycle
    from scripts.trim_active_backlog import apply_plan, plan_manager_trim

    for i, ev in enumerate([100.0, 90.0, 80.0, 70.0]):
        lifecycle.upsert_generated(_task(f"t{i}", ev))

    plan = plan_manager_trim(1, target_active=2)
    assert apply_plan(plan) == 2

    remaining = sorted(d["task_key"] for d in mongo_db["tasks"].find({}, {"_id": 0, "task_key": 1}))
    events = sorted(
        d["task_key"]
        for d in mongo_db["task_events"].find({"kind": "trim_backlog"}, {"_id": 0, "task_key": 1})
    )
    assert remaining == ["t0", "t1"]
    assert events == ["t2", "t3"]


def test_trim_plan_does_not_delete_in_progress_tail(mongo_db):
    from app.services import lifecycle
    from scripts.trim_active_backlog import apply_plan, plan_manager_trim

    for i, ev in enumerate([100.0, 90.0, 80.0]):
        lifecycle.upsert_generated(_task(f"t{i}", ev))
    lifecycle.upsert_generated(_task("protected", 1.0))
    lifecycle.change_status("protected", TaskStatus.IN_PROGRESS, by=1)

    plan = plan_manager_trim(1, target_active=2)
    assert plan["trim_count"] == 1
    assert plan["protected_over_target"] == 1
    assert apply_plan(plan) == 1
    assert mongo_db["tasks"].find_one({"task_key": "protected"}) is not None
