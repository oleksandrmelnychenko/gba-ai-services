"""Head-of-sales dashboard tests — TestClient + mongomock + monkeypatched role/target (no live DB)."""
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


def _target_stub(mid, as_of=None):
    base = {"target": 1000.0, "mtd": 400.0, "daily_pace": 40.0, "expected_to_date": 280.0,
            "gap": -120.0, "today_needed": 30.0, "attainment_pct": 40.0, "pace_status": "ahead"}
    return {"manager_id": mid, "month": "2026-06", "as_of": "2026-06-08",
            "working_days": 26, "working_days_elapsed": 7,
            "shipped": dict(base), "paid": dict(base)}


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
    monkeypatch.setattr(main.signals_repository, "manager_names",
                        lambda ids: {i: f"Manager {i}" for i in ids})

    from app.services import targets
    monkeypatch.setattr(targets, "compute_target", _target_stub)
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


def test_head_sees_team(client):
    resp = client.get("/head/team", params={"manager_net_uid": HEAD_UID})
    assert resp.status_code == 200
    body = resp.json()
    assert body["is_head"] is True
    assert body["as_of"] == "2026-06-08"
    assert [r["manager_id"] for r in body["team"]] == [1, 2]
    row = body["team"][0]
    assert set(row["target"]["shipped"]) == {"target", "mtd", "attainment_pct", "pace_status"}
    assert set(row["tasks"]) == {"active", "generated_month", "done_month", "sold_month",
                                 "dismissed_month", "revenue_month", "close_rate", "conversion_rate"}
    assert body["totals"]["shipped_target"] == 2000.0
    assert body["totals"]["shipped_mtd"] == 800.0
    assert body["totals"]["paid_target"] == 2000.0
    assert "close_rate" in body["totals"] and "conversion_rate" in body["totals"]


def test_non_head_gets_empty_not_403(client):
    # non-head -> 200 {is_head: false} with NO team data (avoids the console's global 403=logout)
    resp = client.get("/head/team", params={"manager_net_uid": MGR_UID})
    assert resp.status_code == 200
    body = resp.json()
    assert body["is_head"] is False
    assert body["team"] == []


def test_unknown_manager_404(client):
    resp = client.get("/head/team", params={"manager_net_uid": UNKNOWN_UID})
    assert resp.status_code == 404
    assert resp.json()["detail"] == "unknown_manager"


def test_team_stats_current_month_only(client):
    from app.data import mongo
    from app.services import lifecycle

    key_now = _seed(1, "mgr:1|client:10|type:debt_followup|win:now")
    lifecycle.change_status(key_now, TaskStatus.DONE, by=1, outcome=Outcome(sold=True, amount=5000))

    key_dismissed = _seed(1, "mgr:1|client:11|type:debt_followup|win:dismiss")
    lifecycle.change_status(key_dismissed, TaskStatus.DISMISSED, by=1)

    key_active = _seed(1, "mgr:1|client:12|type:debt_followup|win:active")

    key_old = _seed(1, "mgr:1|client:13|type:debt_followup|win:old")
    lifecycle.change_status(key_old, TaskStatus.DONE, by=1, outcome=Outcome(sold=True, amount=9000))
    last_month = datetime.now(UTC).replace(day=1) - timedelta(days=2)
    mongo.tasks().update_one({"task_key": key_old}, {"$set": {"updated_at": last_month}})

    stats = lifecycle.team_stats(1)
    assert stats["done_month"] == 1
    assert stats["sold_month"] == 1
    assert stats["revenue_month"] == 5000.0
    assert stats["close_rate"] == 0.5      # done 1 / (done 1 + dismissed 1)
    assert stats["conversion_rate"] == 1.0  # sold 1 / done 1
    assert stats["dismissed_month"] == 1
    assert stats["active"] == 1
    assert key_active  # the open task is the lone active one
