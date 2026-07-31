"""Head-assigned (manual) task tests — creation, permissions, lifecycle immunity, filters."""
from __future__ import annotations

from datetime import timedelta

import mongomock
import pytest
from fastapi.testclient import TestClient

from app.domain.models import Contact, Explanation, Task, TaskType, Urgency

HEAD_UID = "33333333-3333-3333-3333-333333333333"
MGR_UID = "11111111-1111-1111-1111-111111111111"
UNKNOWN_UID = "99999999-9999-9999-9999-999999999999"

_NETUID_MAP = {HEAD_UID: 3, MGR_UID: 1}
_NAMES = {1: "Іван Менеджер", 2: "Петро Другий", 3: "Олена Керівник"}
_CONTACTS = {10: {"client_id": 10, "full_name": "ТОВ Акме", "name": "Акме",
                  "phone": "+380501112233", "email": "acme@example.com"}}


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
                        lambda nu: nu == HEAD_UID)
    monkeypatch.setattr(main.signals_repository, "manager_names",
                        lambda ids: {i: _NAMES[i] for i in ids if i in _NAMES})
    monkeypatch.setattr(main.signals_repository, "contacts_for_clients",
                        lambda ids: {i: _CONTACTS[i] for i in ids if i in _CONTACTS})
    return TestClient(main.app)


def _seed_ai(manager_id: int, key: str, urgency: Urgency = Urgency.CRITICAL,
             client_id: int = 10) -> str:
    from app.services import lifecycle
    task = Task(
        task_key=key, manager_id=manager_id, client_id=client_id, client_name="Acme",
        task_type=TaskType.DEBT_FOLLOWUP, title="Call", reason="overdue",
        priority=90.0, p_outcome=0.9, expected_value=8000.0, ev_score=7200.0,
        urgency=urgency,
        explanation=Explanation(factors=["overdue"], source_signal="debt", confidence=0.9),
        contact=Contact(phone="+380"),
    )
    return lifecycle.upsert_generated(task)


def _create_manual(client, manager_id: int = 1, **overrides) -> dict:
    body = {"manager_id": manager_id, "client_id": 10, "title": "Зустрітись з клієнтом",
            "description": "Обговорити умови на Q3", "urgency": "high", **overrides}
    body = {k: v for k, v in body.items() if v is not None}
    resp = client.post("/head/tasks/new", params={"manager_net_uid": HEAD_UID}, json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_head_creates_manual_task(client):
    doc = _create_manual(client)
    assert doc["task_type"] == "manual"
    assert doc["origin"] == "head"
    assert doc["created_by"] == 3
    assert doc["created_by_name"] == "Олена Керівник"
    assert doc["manager_id"] == 1
    assert doc["manager_name"] == "Іван Менеджер"
    assert doc["client_id"] == 10
    assert doc["client_name"] == "ТОВ Акме"
    assert doc["contact"]["phone"] == "+380501112233"
    assert doc["status"] == "open"
    assert doc["expires_at"] is None
    assert doc["ev_score"] is None
    assert doc["status_history"][0]["by"] == 3


def test_manual_task_pinned_first_in_inbox(client):
    _seed_ai(1, "mgr:1|client:10|type:debt_followup|win:2026-06", urgency=Urgency.CRITICAL)
    _create_manual(client, urgency="normal")
    resp = client.get("/cockpit/inbox", params={"manager_net_uid": MGR_UID})
    assert resp.status_code == 200
    tasks = resp.json()["tasks"]
    assert len(tasks) == 2
    assert tasks[0]["task_type"] == "manual"  # pinned above a critical AI debt


def test_non_head_create_forbidden(client):
    resp = client.post("/head/tasks/new", params={"manager_net_uid": MGR_UID},
                       json={"manager_id": 1, "title": "x"})
    assert resp.status_code == 403


def test_create_unknown_manager_404(client):
    resp = client.post("/head/tasks/new", params={"manager_net_uid": HEAD_UID},
                       json={"manager_id": 77, "title": "x"})
    assert resp.status_code == 404
    assert resp.json()["detail"] == "unknown_target_manager"


def test_create_unknown_client_404(client):
    resp = client.post("/head/tasks/new", params={"manager_net_uid": HEAD_UID},
                       json={"manager_id": 1, "client_id": 777, "title": "x"})
    assert resp.status_code == 404
    assert resp.json()["detail"] == "unknown_client"


def test_create_without_client(client):
    doc = _create_manual(client, client_id=None, title="Здати звіт по відділу")
    assert doc["client_id"] == 0
    assert doc["client_name"] is None
    assert doc["contact"]["phone"] is None


def test_manager_dismisses_with_reason(client):
    key = _create_manual(client)["task_key"]
    resp = client.post("/cockpit/status", params={"manager_net_uid": MGR_UID},
                       json={"task_key": key, "to": "dismissed", "reason": "Не актуально"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "dismissed"
    assert body["status_history"][-1]["reason"] == "Не актуально"


def test_head_can_cancel_own_manual_but_not_ai_task(client):
    manual_key = _create_manual(client)["task_key"]
    ai_key = _seed_ai(1, "mgr:1|client:10|type:debt_followup|win:2026-06")
    ok = client.post("/cockpit/status", params={"manager_net_uid": HEAD_UID},
                     json={"task_key": manual_key, "to": "dismissed", "reason": "скасовано"})
    assert ok.status_code == 200
    denied = client.post("/cockpit/status", params={"manager_net_uid": HEAD_UID},
                         json={"task_key": ai_key, "to": "dismissed"})
    assert denied.status_code == 403


def test_head_notes_any_task_with_author_name(client):
    ai_key = _seed_ai(1, "mgr:1|client:10|type:debt_followup|win:2026-06")
    resp = client.post("/cockpit/notes", params={"manager_net_uid": HEAD_UID},
                       json={"task_key": ai_key, "text": "перевір це сьогодні"})
    assert resp.status_code == 200
    note = resp.json()["notes"][-1]
    assert note["author_id"] == 3
    assert note["author_name"] == "Олена Керівник"


def test_sweep_expired_never_deletes_manual(client):
    from app.data import mongo as m
    from app.services import lifecycle
    manual_key = _create_manual(client)["task_key"]
    ai_key = _seed_ai(1, "mgr:1|client:10|type:debt_followup|win:2026-06")
    past = lifecycle._now() - timedelta(days=1)
    m.tasks().update_one({"task_key": ai_key}, {"$set": {"expires_at": past}})
    deleted = lifecycle.sweep_expired()
    assert deleted == 1
    assert lifecycle.get_task(manual_key) is not None
    assert lifecycle.get_task(ai_key) is None


def test_sweep_orphaned_skips_manual(client, monkeypatch):
    from app.data import signals_repository
    from app.services import lifecycle
    _create_manual(client, client_id=None)          # client_id=0 — no dbo.Client row by design
    ai_key = _seed_ai(1, "mgr:1|client:55|type:debt_followup|win:2026-06", client_id=55)
    monkeypatch.setattr(signals_repository, "existing_client_ids", lambda ids: set())
    orphaned = lifecycle.sweep_orphaned()
    assert orphaned == 1
    assert lifecycle.get_task(ai_key)["status"] == "orphaned"
    inbox = lifecycle.inbox(1)
    assert [t["task_type"] for t in inbox] == ["manual"]


def test_head_clients_gate_and_listing(client, monkeypatch):
    from app.api import main
    monkeypatch.setattr(main.signals_repository, "clients_for_manager",
                        lambda mid: [{"client_id": 10, "full_name": "ТОВ Акме", "name": "Акме",
                                      "phone": None, "email": None}])
    benign = client.get("/head/clients", params={"manager_net_uid": MGR_UID, "manager_id": 1})
    assert benign.status_code == 200
    assert benign.json() == {"is_head": False, "manager_id": 1, "count": 0, "clients": []}
    ok = client.get("/head/clients", params={"manager_net_uid": HEAD_UID, "manager_id": 1})
    assert ok.status_code == 200
    assert ok.json()["count"] == 1
    assert ok.json()["clients"][0]["client_id"] == 10


def test_cockpit_clients_merges_debt(client, monkeypatch):
    from app.api import main
    monkeypatch.setattr(
        main.signals_repository, "clients_overview_for_manager",
        lambda mid, as_of: [
            {"client_id": 10, "client_net_uid": "aaaa", "name": "Акме", "full_name": "ТОВ Акме",
             "phone": None, "email": None, "last_order": None, "orders_cnt": 4,
             "turnover_eur": 1234.567},
            {"client_id": 11, "client_net_uid": "bbbb", "name": "Бета", "full_name": "ТОВ Бета",
             "phone": None, "email": None, "last_order": None, "orders_cnt": 0,
             "turnover_eur": 0},
        ])
    monkeypatch.setattr(
        main.signals_repository, "overdue_debts_for_manager",
        lambda mid, as_of: [{"client_id": 10, "overdue_amount": 500.129,
                             "max_overdue_days": 40, "max_days_past_terms": 12,
                             "debt_lines": 2}])
    resp = client.get("/cockpit/clients", params={"manager_net_uid": MGR_UID})
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 2
    first = body["clients"][0]
    assert first["overdue_eur"] == 500.13
    assert first["max_days_past_terms"] == 12
    assert first["turnover_eur"] == 1234.57
    assert body["clients"][1]["overdue_eur"] == 0.0


def test_head_board_task_type_filter(client, monkeypatch):
    from app.api import main
    monkeypatch.setattr(main.signals_repository, "all_managers", lambda: [1, 2])
    _seed_ai(1, "mgr:1|client:10|type:debt_followup|win:2026-06")
    _create_manual(client)
    resp = client.get("/head/tasks", params={"manager_net_uid": HEAD_UID, "task_type": "manual"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["tasks"][0]["task_type"] == "manual"
    assert body["tasks"][0]["origin"] == "head"
    assert body["tasks"][0]["created_by"] == 3
    bad = client.get("/head/tasks", params={"manager_net_uid": HEAD_UID, "task_type": "nope"})
    assert bad.status_code == 422


def test_head_board_surfaces_dismiss_reason_and_outcome(client, monkeypatch):
    from app.api import main
    monkeypatch.setattr(main.signals_repository, "all_managers", lambda: [1, 2])
    manual_key = _create_manual(client)["task_key"]
    done_key = _seed_ai(1, "mgr:1|client:10|type:debt_followup|win:2026-06")
    client.post("/cockpit/status", params={"manager_net_uid": MGR_UID},
                json={"task_key": manual_key, "to": "dismissed", "reason": "Клієнт відмовився"})
    client.post("/cockpit/status", params={"manager_net_uid": MGR_UID},
                json={"task_key": done_key, "to": "done", "sold": True, "amount": 700})
    resp = client.get("/head/tasks", params={"manager_net_uid": HEAD_UID,
                                             "statuses": "dismissed,done"})
    assert resp.status_code == 200
    rows = {t["task_key"]: t for t in resp.json()["tasks"]}
    assert rows[manual_key]["resolution_reason"] == "Клієнт відмовився"
    assert rows[done_key]["outcome"]["sold"] is True
    assert rows[done_key]["outcome"]["amount"] == 700


def test_head_dismissals_aggregation(client, monkeypatch):
    from app.api import main
    monkeypatch.setattr(main.signals_repository, "all_managers", lambda: [1, 2])
    k1 = _seed_ai(1, "mgr:1|client:10|type:debt_followup|win:2026-06")
    k2 = _seed_ai(1, "mgr:1|client:11|type:debt_followup|win:2026-06", client_id=11)
    manual_key = _create_manual(client)["task_key"]
    client.post("/cockpit/status", params={"manager_net_uid": MGR_UID},
                json={"task_key": k1, "to": "dismissed", "reason": "Ціна зависока"})
    client.post("/cockpit/status", params={"manager_net_uid": MGR_UID},
                json={"task_key": k2, "to": "dismissed", "reason": "ціна  зависока"})
    client.post("/cockpit/status", params={"manager_net_uid": MGR_UID},
                json={"task_key": manual_key, "to": "dismissed"})

    resp = client.get("/head/dismissals",
                      params={"manager_net_uid": HEAD_UID, "window_days": 30})
    assert resp.status_code == 200
    data = resp.json()
    assert data["is_head"] is True
    assert data["window_days"] == 30
    assert data["total_dismissed"] == 3
    assert len(data["managers"]) == 1
    mgr = data["managers"][0]
    assert mgr["manager_id"] == 1
    assert mgr["manager_name"] == "Іван Менеджер"
    assert mgr["dismissed"] == 3
    assert mgr["manual"] == 1
    assert mgr["no_reason"] == 1
    # case/whitespace variants of the same wording collapse into one bucket
    assert mgr["reasons"] == [{"reason": "Ціна зависока", "count": 2}]
    assert data["top_reasons"] == [{"reason": "Ціна зависока", "count": 2, "managers": 1}]


def test_head_dismissals_non_head_is_benign(client):
    resp = client.get("/head/dismissals", params={"manager_net_uid": MGR_UID})
    assert resp.status_code == 200
    data = resp.json()
    assert data["is_head"] is False
    assert data["total_dismissed"] == 0
    assert data["managers"] == []
