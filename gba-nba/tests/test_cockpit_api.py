"""Cockpit API tests — TestClient + mongomock + monkeypatched manager resolution (no live DB)."""
from __future__ import annotations

import mongomock
import pytest
from fastapi.testclient import TestClient

from app.domain.models import Contact, Explanation, Task, TaskType, Urgency

MGR_UID = "11111111-1111-1111-1111-111111111111"
OTHER_UID = "22222222-2222-2222-2222-222222222222"
UNKNOWN_UID = "99999999-9999-9999-9999-999999999999"

_NETUID_MAP = {MGR_UID: 1, OTHER_UID: 2}


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
    return TestClient(main.app)


def _seed(manager_id: int, key: str, urgency: Urgency = Urgency.HIGH) -> str:
    from app.services import lifecycle
    task = Task(
        task_key=key, manager_id=manager_id, client_id=10, client_name="Acme",
        task_type=TaskType.DEBT_FOLLOWUP, title="Call", reason="overdue",
        priority=80.0, urgency=urgency,
        explanation=Explanation(factors=["overdue 12d"], source_signal="debt", confidence=0.8),
        contact=Contact(phone="+380"),
    )
    return lifecycle.upsert_generated(task)


def test_inbox_returns_only_that_managers_tasks(client):
    _seed(1, "mgr:1|client:10|type:debt_followup|win:2026-06")
    _seed(2, "mgr:2|client:10|type:debt_followup|win:2026-06")
    resp = client.get("/cockpit/inbox", params={"manager_net_uid": MGR_UID})
    assert resp.status_code == 200
    body = resp.json()
    assert body["manager_id"] == 1
    assert body["manager_net_uid"] == MGR_UID
    assert body["count"] == 1
    assert body["tasks"][0]["manager_id"] == 1
    assert isinstance(body["tasks"][0]["_id"], str)


def test_count_shape(client):
    _seed(1, "mgr:1|client:10|type:debt_followup|win:2026-06", urgency=Urgency.CRITICAL)
    _seed(1, "mgr:1|client:11|type:debt_followup|win:2026-06", urgency=Urgency.HIGH)
    resp = client.get("/cockpit/count", params={"manager_net_uid": MGR_UID})
    assert resp.status_code == 200
    body = resp.json()
    assert body["manager_id"] == 1
    assert body["active_count"] == 2
    assert body["by_urgency"] == {"critical": 1, "high": 1, "normal": 0, "low": 0}


def test_status_happy_path(client):
    key = _seed(1, "mgr:1|client:10|type:debt_followup|win:2026-06")
    resp = client.post("/cockpit/status", params={"manager_net_uid": MGR_UID},
                       json={"task_key": key, "to": "done", "sold": True, "amount": 5000})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "done"
    assert body["outcome"]["sold"] is True
    assert body["outcome"]["amount"] == 5000


def test_status_ownership_403(client):
    key = _seed(2, "mgr:2|client:10|type:debt_followup|win:2026-06")
    resp = client.post("/cockpit/status", params={"manager_net_uid": MGR_UID},
                       json={"task_key": key, "to": "done"})
    assert resp.status_code == 403


def test_unknown_manager_404(client):
    resp = client.get("/cockpit/inbox", params={"manager_net_uid": UNKNOWN_UID})
    assert resp.status_code == 404
    assert resp.json()["detail"] == "unknown_manager"


def test_illegal_transition_400(client):
    from app.domain.models import TaskStatus
    from app.services import lifecycle
    key = _seed(1, "mgr:1|client:10|type:debt_followup|win:2026-06")
    lifecycle.change_status(key, TaskStatus.SNOOZED, by=1)
    resp = client.post("/cockpit/status", params={"manager_net_uid": MGR_UID},
                       json={"task_key": key, "to": "in_progress"})
    assert resp.status_code == 400


def test_notes_happy_path(client):
    key = _seed(1, "mgr:1|client:10|type:debt_followup|win:2026-06")
    resp = client.post("/cockpit/notes", params={"manager_net_uid": MGR_UID},
                       json={"task_key": key, "text": "call friday"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["notes"][-1]["text"] == "call friday"
    assert body["notes"][-1]["author_id"] == 1


def test_notes_ownership_403(client):
    key = _seed(2, "mgr:2|client:10|type:debt_followup|win:2026-06")
    resp = client.post("/cockpit/notes", params={"manager_net_uid": MGR_UID},
                       json={"task_key": key, "text": "nope"})
    assert resp.status_code == 403


def test_malformed_netuid_404(client):
    resp = client.get("/cockpit/inbox", params={"manager_net_uid": "not-a-guid"})
    assert resp.status_code == 404
    assert resp.json()["detail"] == "unknown_manager"


def test_internal_key_required_when_configured(client, monkeypatch):
    from app.api import main
    monkeypatch.setattr(main.settings, "internal_api_key", "s3cret")
    # no header -> rejected
    assert client.get("/cockpit/count", params={"manager_net_uid": MGR_UID}).status_code == 401
    # wrong header -> rejected
    assert client.get("/cockpit/count", params={"manager_net_uid": MGR_UID},
                      headers={"X-Internal-Api-Key": "wrong"}).status_code == 401
    # correct header -> allowed
    assert client.get("/cockpit/count", params={"manager_net_uid": MGR_UID},
                      headers={"X-Internal-Api-Key": "s3cret"}).status_code == 200
    # health stays open without the key
    assert client.get("/health").status_code == 200
    # the global gate also covers the legacy endpoints (no header -> 401)
    assert client.get("/tasks/manager/1").status_code == 401
