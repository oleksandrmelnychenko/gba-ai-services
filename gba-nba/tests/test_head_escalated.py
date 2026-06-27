"""Head-of-sales escalation queue tests — TestClient + mongomock + monkeypatched role (no live DB)."""
from __future__ import annotations

import mongomock
import pytest
from fastapi.testclient import TestClient

from app.domain.models import (
    Contact,
    Explanation,
    Task,
    TaskType,
    Urgency,
)

HEAD_UID = "10101010-1010-1010-1010-101010101010"
MGR_UID = "11111111-1111-1111-1111-111111111111"
UNKNOWN_UID = "99999999-9999-9999-9999-999999999999"

_NETUID_MAP = {HEAD_UID: 99, MGR_UID: 1}
_HEADS = {HEAD_UID}


@pytest.fixture
def client(monkeypatch):
    mongo_client = mongomock.MongoClient()
    db = mongo_client["gba_nba_test"]
    from app.data import mongo as m
    monkeypatch.setattr(m, "get_client", lambda: mongo_client)
    monkeypatch.setattr(m, "get_db", lambda: db)
    monkeypatch.setattr(m, "tasks", lambda: db["tasks"])
    monkeypatch.setattr(m, "task_events", lambda: db["task_events"])
    monkeypatch.setattr(m, "manager_prefs", lambda: db["manager_prefs"])

    from app.api import main
    monkeypatch.setattr(main.signals_repository, "manager_id_for_netuid",
                        lambda nu: _NETUID_MAP.get(nu))
    monkeypatch.setattr(main.signals_repository, "is_head_of_sales",
                        lambda nu: nu in _HEADS)
    return TestClient(main.app)


def _seed(key: str, manager_id: int = 1,
          task_type: TaskType = TaskType.DEBT_FOLLOWUP,
          urgency: Urgency = Urgency.HIGH, priority: float = 80.0,
          ev_score: float = 0.0,
          escalated_to: int | None = None) -> str:
    from app.data import mongo
    from app.services import lifecycle
    task = Task(
        task_key=key, manager_id=manager_id, client_id=10, client_name="Acme",
        task_type=task_type, title="Call", reason="overdue",
        priority=priority, ev_score=ev_score, urgency=urgency,
        explanation=Explanation(factors=["overdue 12d"], source_signal="debt", confidence=0.8),
        contact=Contact(phone="+380"),
    )
    lifecycle.upsert_generated(task)
    if escalated_to is not None:
        mongo.tasks().update_one({"task_key": key}, {"$set": {"escalated_to": escalated_to}})
    return key


def test_head_sees_only_escalated(client):
    _seed("mgr:1|client:10|type:debt_followup|win:esc", escalated_to=99)
    _seed("mgr:1|client:11|type:debt_followup|win:plain")

    resp = client.get("/head/escalated", params={"manager_net_uid": HEAD_UID})
    assert resp.status_code == 200
    body = resp.json()
    assert body["is_head"] is True
    assert body["count"] == 1
    keys = [t["task_key"] for t in body["tasks"]]
    assert keys == ["mgr:1|client:10|type:debt_followup|win:esc"]
    assert isinstance(body["tasks"][0]["_id"], str)


def test_non_head_gets_empty_not_403(client):
    _seed("mgr:1|client:10|type:debt_followup|win:esc", escalated_to=99)
    resp = client.get("/head/escalated", params={"manager_net_uid": MGR_UID})
    assert resp.status_code == 200
    body = resp.json()
    assert body["is_head"] is False
    assert body["count"] == 0
    assert body["tasks"] == []


def test_unknown_manager_404(client):
    resp = client.get("/head/escalated", params={"manager_net_uid": UNKNOWN_UID})
    assert resp.status_code == 404
    assert resp.json()["detail"] == "unknown_manager"


def test_escalated_ordering(client):
    # urgency band -> EV desc -> type tie-breaker -> priority desc (mirrors the manager inbox sort)
    _seed("k|high|debt|low_pri", urgency=Urgency.HIGH, task_type=TaskType.DEBT_FOLLOWUP,
          priority=10.0, escalated_to=99)
    _seed("k|crit|reorder", urgency=Urgency.CRITICAL, task_type=TaskType.REORDER_DUE,
          priority=5.0, escalated_to=99)
    _seed("k|high|debt|high_pri", urgency=Urgency.HIGH, task_type=TaskType.DEBT_FOLLOWUP,
          priority=90.0, escalated_to=99)
    _seed("k|high|reorder", urgency=Urgency.HIGH, task_type=TaskType.REORDER_DUE,
          priority=99.0, escalated_to=99)

    resp = client.get("/head/escalated", params={"manager_net_uid": HEAD_UID})
    body = resp.json()
    keys = [t["task_key"] for t in body["tasks"]]
    assert keys == ["k|crit|reorder", "k|high|debt|high_pri", "k|high|debt|low_pri", "k|high|reorder"]


def test_escalated_ordering_uses_ev_and_sinks_unvalued_legacy(client):
    high_ev = _seed("k|high|debt|high_ev", urgency=Urgency.HIGH, task_type=TaskType.DEBT_FOLLOWUP,
                    priority=20.0, ev_score=900.0, escalated_to=99)
    high_probability = _seed("k|high|debt|high_probability", urgency=Urgency.HIGH,
                             task_type=TaskType.DEBT_FOLLOWUP, priority=95.0, ev_score=40.0,
                             escalated_to=99)
    legacy = _seed("k|high|debt|legacy", urgency=Urgency.HIGH, task_type=TaskType.DEBT_FOLLOWUP,
                   priority=80.0, escalated_to=99)
    from app.data import mongo
    mongo.tasks().update_one(
        {"task_key": legacy},
        {"$unset": {"p_outcome": "", "expected_value": "", "ev_score": ""}},
    )

    resp = client.get("/head/escalated", params={"manager_net_uid": HEAD_UID})
    keys = [t["task_key"] for t in resp.json()["tasks"]]

    assert keys[:3] == [high_ev, high_probability, legacy]


def test_limit_respected(client):
    for i in range(5):
        _seed(f"k|{i}", priority=float(i), escalated_to=99)
    resp = client.get("/head/escalated", params={"manager_net_uid": HEAD_UID, "limit": 2})
    body = resp.json()
    assert body["count"] == 2
