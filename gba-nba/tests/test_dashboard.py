"""Dashboard chart-data endpoint tests — TestClient + mongomock + monkeypatched signals (no live DB).

Covers GET /cockpit/dashboard (manager) and GET /cockpit/head/dashboard (head/team), plus the
EUR-correct debt aggregation (value_at_risk + aging buckets) the manager DTO is built from.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import mongomock
import pytest
from fastapi.testclient import TestClient

from app.domain.models import (
    Contact,
    Explanation,
    Outcome,
    Task,
    TaskStatus,
    TaskType,
    Urgency,
)

HEAD_UID = "10101010-1010-1010-1010-101010101010"
MGR_UID = "11111111-1111-1111-1111-111111111111"
UNKNOWN_UID = "99999999-9999-9999-9999-999999999999"

_NETUID_MAP = {HEAD_UID: 99, MGR_UID: 1}
_HEADS = {HEAD_UID}

# canned EUR-correct debt aggregation per manager (so the endpoints never touch the live DB)
_DEBT = {
    1: {"value_at_risk_eur": 1234.50,
        "debt_aging": [{"bucket": "0-30", "amount_eur": 234.5, "count": 1},
                       {"bucket": "31-60", "amount_eur": 0.0, "count": 0},
                       {"bucket": "61-90", "amount_eur": 0.0, "count": 0},
                       {"bucket": "90+", "amount_eur": 1000.0, "count": 1}]},
    2: {"value_at_risk_eur": 500.0,
        "debt_aging": [{"bucket": "0-30", "amount_eur": 500.0, "count": 1},
                       {"bucket": "31-60", "amount_eur": 0.0, "count": 0},
                       {"bucket": "61-90", "amount_eur": 0.0, "count": 0},
                       {"bucket": "90+", "amount_eur": 0.0, "count": 0}]},
}


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
    monkeypatch.setattr(main.signals_repository, "all_managers", lambda: [1, 2])
    monkeypatch.setattr(main.signals_repository, "debt_dashboard_for_manager",
                        lambda mid, as_of: _DEBT.get(mid, {"value_at_risk_eur": 0.0, "debt_aging": []}))
    return TestClient(main.app)


def _seed(manager_id: int, key: str, task_type: TaskType = TaskType.DEBT_FOLLOWUP,
          urgency: Urgency = Urgency.HIGH) -> str:
    from app.services import lifecycle
    task = Task(
        task_key=key, manager_id=manager_id, client_id=10, client_name="Acme",
        task_type=task_type, title="Call", reason="overdue", priority=80.0, urgency=urgency,
        explanation=Explanation(factors=["x"], source_signal="debt", confidence=0.8),
        contact=Contact(phone="+380"),
    )
    return lifecycle.upsert_generated(task)


# ---------------- manager dashboard ----------------

def test_manager_dashboard_shape_and_mix(client):
    _seed(1, "k|c10|debt", task_type=TaskType.DEBT_FOLLOWUP, urgency=Urgency.CRITICAL)
    _seed(1, "k|c11|debt", task_type=TaskType.DEBT_FOLLOWUP, urgency=Urgency.HIGH)
    _seed(1, "k|c12|reorder", task_type=TaskType.REORDER_DUE, urgency=Urgency.NORMAL)
    _seed(2, "k|c10|debt|other", task_type=TaskType.DEBT_FOLLOWUP)  # other manager -> excluded

    resp = client.get("/cockpit/dashboard", params={"manager_net_uid": MGR_UID})
    assert resp.status_code == 200
    body = resp.json()
    assert body["manager_id"] == 1
    assert set(body) == {"manager_id", "as_of", "task_type_mix", "urgency_mix",
                         "value_at_risk_eur", "debt_aging", "completed_vs_open"}

    type_mix = {r["type"]: r["count"] for r in body["task_type_mix"]}
    assert type_mix == {"debt_followup": 2, "reorder_due": 1}

    urgency_mix = {r["urgency"]: r["count"] for r in body["urgency_mix"]}
    assert urgency_mix == {"critical": 1, "high": 1, "normal": 1, "low": 0}
    assert [r["urgency"] for r in body["urgency_mix"]] == ["critical", "high", "normal", "low"]

    assert body["value_at_risk_eur"] == 1234.50
    assert body["debt_aging"] == _DEBT[1]["debt_aging"]


def test_manager_dashboard_completed_vs_open(client):
    open_key = _seed(1, "k|open")
    done_key = _seed(1, "k|done")
    from app.services import lifecycle
    lifecycle.change_status(done_key, TaskStatus.DONE, by=1, outcome=Outcome(sold=True, amount=10))
    dis_key = _seed(1, "k|dis")
    lifecycle.change_status(dis_key, TaskStatus.DISMISSED, by=1)

    resp = client.get("/cockpit/dashboard", params={"manager_net_uid": MGR_UID})
    cvo = {r["status"]: r["count"] for r in resp.json()["completed_vs_open"]}
    assert cvo == {"open": 1, "done": 1, "dismissed": 1}
    assert open_key  # the lone open task


def test_manager_dashboard_done_last_month_excluded(client):
    from app.data import mongo
    from app.services import lifecycle
    old = _seed(1, "k|old")
    lifecycle.change_status(old, TaskStatus.DONE, by=1, outcome=Outcome(sold=True, amount=99))
    last_month = datetime.now(UTC).replace(day=1) - timedelta(days=2)
    mongo.tasks().update_one({"task_key": old}, {"$set": {"updated_at": last_month}})

    resp = client.get("/cockpit/dashboard", params={"manager_net_uid": MGR_UID})
    cvo = {r["status"]: r["count"] for r in resp.json()["completed_vs_open"]}
    assert cvo["done"] == 0  # resolved last month -> not in this month's window


def test_manager_dashboard_unknown_404(client):
    assert client.get("/cockpit/dashboard",
                      params={"manager_net_uid": UNKNOWN_UID}).status_code == 404


# ---------------- head dashboard ----------------

def test_head_dashboard_shape(client):
    _seed(1, "k|m1a", urgency=Urgency.CRITICAL)
    _seed(1, "k|m1b", urgency=Urgency.HIGH)
    _seed(2, "k|m2a", urgency=Urgency.NORMAL)
    from app.data import mongo
    mongo.tasks().update_one({"task_key": "k|m1a"}, {"$set": {"escalated_to": 99}})

    resp = client.get("/cockpit/head/dashboard", params={"manager_net_uid": HEAD_UID})
    assert resp.status_code == 200
    body = resp.json()
    assert body["is_head"] is True
    assert set(body) == {"is_head", "as_of", "teams", "escalated_count", "total_value_at_risk_eur"}
    teams = {t["manager_id"]: t for t in body["teams"]}
    assert set(teams[1]) == {"manager_id", "open_tasks", "critical", "value_at_risk_eur"}
    assert teams[1]["open_tasks"] == 2
    assert teams[1]["critical"] == 1
    assert teams[1]["value_at_risk_eur"] == 1234.50
    assert teams[2]["open_tasks"] == 1
    assert teams[2]["critical"] == 0
    assert teams[2]["value_at_risk_eur"] == 500.0
    assert body["escalated_count"] == 1
    assert body["total_value_at_risk_eur"] == 1734.50


def test_head_dashboard_non_head_benign(client):
    resp = client.get("/cockpit/head/dashboard", params={"manager_net_uid": MGR_UID})
    assert resp.status_code == 200
    body = resp.json()
    assert body["is_head"] is False
    assert body["teams"] == []
    assert body["escalated_count"] == 0
    assert body["total_value_at_risk_eur"] == 0.0


def test_head_dashboard_unknown_404(client):
    assert client.get("/cockpit/head/dashboard",
                      params={"manager_net_uid": UNKNOWN_UID}).status_code == 404


def test_dashboard_requires_internal_key(client, monkeypatch):
    from app.api import main
    monkeypatch.setattr(main.settings, "internal_api_key", "s3cret")
    assert client.get("/cockpit/dashboard",
                      params={"manager_net_uid": MGR_UID}).status_code == 401
    assert client.get("/cockpit/head/dashboard",
                      params={"manager_net_uid": HEAD_UID}).status_code == 401
    assert client.get("/cockpit/dashboard", params={"manager_net_uid": MGR_UID},
                      headers={"X-Internal-Api-Key": "s3cret"}).status_code == 200
    assert client.get("/cockpit/head/dashboard", params={"manager_net_uid": HEAD_UID},
                      headers={"X-Internal-Api-Key": "s3cret"}).status_code == 200


# ---------------- EUR-correct debt aggregation (unit) ----------------

def test_debt_dashboard_aging_buckets(monkeypatch):
    from app.data import signals_repository as sig
    rows = [
        {"client_id": 1, "overdue_amount": 100.0, "max_overdue_days": 10},   # 0-30
        {"client_id": 2, "overdue_amount": 200.0, "max_overdue_days": 30},   # 0-30 (boundary)
        {"client_id": 3, "overdue_amount": 50.0, "max_overdue_days": 45},    # 31-60
        {"client_id": 4, "overdue_amount": 75.0, "max_overdue_days": 90},    # 61-90 (boundary)
        {"client_id": 5, "overdue_amount": 1000.0, "max_overdue_days": 365}, # 90+
    ]
    monkeypatch.setattr(sig, "overdue_debts_for_manager", lambda mid, as_of, **kw: rows)
    out = sig.debt_dashboard_for_manager(7, "2026-06-17")
    assert out["value_at_risk_eur"] == 1425.0
    aging = {b["bucket"]: b for b in out["debt_aging"]}
    assert aging["0-30"] == {"bucket": "0-30", "amount_eur": 300.0, "count": 2}
    assert aging["31-60"] == {"bucket": "31-60", "amount_eur": 50.0, "count": 1}
    assert aging["61-90"] == {"bucket": "61-90", "amount_eur": 75.0, "count": 1}
    assert aging["90+"] == {"bucket": "90+", "amount_eur": 1000.0, "count": 1}
