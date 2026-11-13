"""Cross-service feedback: NBA pushes negative cross_sell signals to reco so it stops
recommending products managers dismiss / fail to sell."""
from __future__ import annotations

import mongomock
import pytest

from app.domain.models import Outcome, Task, TaskStatus, TaskType, Urgency


@pytest.fixture
def mongo_db(monkeypatch):
    mc = mongomock.MongoClient()
    db = mc["gba_nba_test"]
    from app.data import mongo as m
    monkeypatch.setattr(m, "get_client", lambda: mc)
    monkeypatch.setattr(m, "get_db", lambda: db)
    monkeypatch.setattr(m, "tasks", lambda: db["tasks"])
    monkeypatch.setattr(m, "task_events", lambda: db["task_events"])
    monkeypatch.setattr(m, "manager_prefs", lambda: db["manager_prefs"])
    return db


def _xsell(key: str, client_id: int, pids: list[int]) -> Task:
    return Task(
        task_key=key, manager_id=1, client_id=client_id, task_type=TaskType.CROSS_SELL,
        title="x", reason="r", priority=50.0, urgency=Urgency.NORMAL,
        payload={"products": [{"product_id": p, "name": "n"} for p in pids]},
    )


def test_cross_sell_negatives_collects_dismissed_and_not_sold(mongo_db):
    from app.services import lifecycle
    lifecycle.upsert_generated(_xsell("k1", 100, [11, 12]))
    lifecycle.change_status("k1", TaskStatus.DISMISSED, by=1)                   # dismissed -> negative
    lifecycle.upsert_generated(_xsell("k2", 100, [12, 13]))
    lifecycle.change_status("k2", TaskStatus.DONE, by=1, outcome=Outcome(sold=False))   # not sold -> neg
    lifecycle.upsert_generated(_xsell("k3", 200, [21]))
    lifecycle.change_status("k3", TaskStatus.DONE, by=1, outcome=Outcome(sold=True, amount=9.0))   # sold

    negs = lifecycle.cross_sell_negatives()
    assert negs[100] == {11, 12, 13}      # union across this client's rejected cross_sell tasks
    assert 200 not in negs                # the sold one is not a negative


def test_cross_sell_negatives_ignores_other_task_types(mongo_db):
    from app.domain.models import Task as T
    from app.services import lifecycle
    debt = T(task_key="d1", manager_id=1, client_id=300, task_type=TaskType.DEBT_FOLLOWUP,
             title="t", reason="r", priority=80.0, urgency=Urgency.HIGH,
             payload={"debt": {"overdue_amount": 100}})
    lifecycle.upsert_generated(debt)
    lifecycle.change_status("d1", TaskStatus.DISMISSED, by=1)
    assert lifecycle.cross_sell_negatives() == {}     # debt dismissals are not reco feedback


def test_push_reco_feedback_sends_per_client(monkeypatch):
    from app.services import worker
    monkeypatch.setattr(worker.lifecycle, "cross_sell_negatives",
                        lambda window_days: {100: {11, 12}, 200: {21}})
    sent = []
    monkeypatch.setattr(worker.reco_client, "send_feedback",
                        lambda cid, pids, kind="reject": sent.append((cid, tuple(pids))) or True)
    out = worker.push_reco_feedback(window_days=90)
    assert out == {"clients": 2, "sent": 2, "products": 3}
    assert (100, (11, 12)) in sent and (200, (21,)) in sent


def test_push_reco_feedback_graceful_when_reco_down(monkeypatch):
    from app.services import worker
    monkeypatch.setattr(worker.lifecycle, "cross_sell_negatives", lambda window_days: {100: {11}})
    monkeypatch.setattr(worker.reco_client, "send_feedback", lambda cid, pids, kind="reject": False)
    out = worker.push_reco_feedback(window_days=90)
    assert out == {"clients": 1, "sent": 0, "products": 0}    # counted as attempted, nothing sent
