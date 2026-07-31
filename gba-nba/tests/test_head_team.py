"""Head-of-sales dashboard tests — TestClient + mongomock + monkeypatched role/target (no live DB)."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

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
    resolved_as_of = as_of or "2026-06-08"
    base = {"target": 1000.0, "mtd": 400.0, "daily_pace": 40.0, "expected_to_date": 280.0,
            "gap": -120.0, "today_needed": 30.0, "attainment_pct": 40.0, "pace_status": "ahead"}
    return {"manager_id": mid, "month": resolved_as_of[:7], "as_of": resolved_as_of,
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
    monkeypatch.setattr(main, "_kyiv_today", lambda: date(2026, 6, 8))
    monkeypatch.setattr(main, "_team_snap_state", {"at": 0.0, "as_of": None, "value": None})
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
    assert body["requested_manager_net_uid"] == HEAD_UID.lower()
    assert body["as_of"] == "2026-06-08"
    assert body["expected_manager_count"] == 2
    assert body["returned_manager_count"] == 2
    assert [r["manager_id"] for r in body["team"]] == [1, 2]
    row = body["team"][0]
    assert set(row["target"]["shipped"]) == {
        "target",
        "mtd",
        "expected_to_date",
        "attainment_pct",
        "pace_status",
    }
    assert set(row["tasks"]) == {"active", "generated_month", "done_month", "sold_month",
                                 "dismissed_month", "revenue_month", "close_rate", "conversion_rate"}
    assert body["totals"]["shipped_target"] == 2000.0
    assert body["totals"]["shipped_mtd"] == 800.0
    assert body["totals"]["paid_target"] == 2000.0
    assert "close_rate" in body["totals"] and "conversion_rate" in body["totals"]


def test_explicit_current_date_reuses_same_snapshot_for_team_and_targets(client, monkeypatch):
    from app.api import main
    from app.services import targets

    monkeypatch.setattr(main, "_kyiv_today", lambda: date(2026, 8, 1))
    target_calls = []
    stats_calls = []

    def compute_target(manager_id, as_of=None):
        target_calls.append((manager_id, as_of))
        return _target_stub(manager_id, as_of)

    def team_stats(manager_id, as_of):
        stats_calls.append((manager_id, as_of))
        return {
            "active": 0,
            "generated_month": 0,
            "done_month": 0,
            "sold_month": 0,
            "dismissed_month": 0,
            "revenue_month": 0.0,
            "close_rate": 0.0,
            "conversion_rate": 0.0,
        }

    monkeypatch.setattr(targets, "compute_target", compute_target)
    monkeypatch.setattr(main.lifecycle, "team_stats", team_stats)

    params = {"manager_net_uid": HEAD_UID, "as_of_date": "2026-08-01"}
    team_response = client.get("/head/team", params=params)
    target_response = client.get("/targets/overview", params=params)

    assert team_response.status_code == 200
    assert target_response.status_code == 200
    assert team_response.json()["as_of"] == "2026-08-01"
    assert target_calls == [(1, "2026-08-01"), (2, "2026-08-01")]
    assert stats_calls == [(1, "2026-08-01"), (2, "2026-08-01")]


def test_non_head_gets_empty_not_403(client):
    # non-head -> 200 {is_head: false} with NO team data (avoids the console's global 403=logout)
    resp = client.get("/head/team", params={"manager_net_uid": MGR_UID})
    assert resp.status_code == 200
    body = resp.json()
    assert body["is_head"] is False
    assert body["requested_manager_net_uid"] == MGR_UID.lower()
    assert body["expected_manager_count"] == 0
    assert body["returned_manager_count"] == 0
    assert body["team"] == []
    assert body["totals"] == {
        "shipped_target": 0.0,
        "shipped_mtd": 0.0,
        "paid_target": 0.0,
        "paid_mtd": 0.0,
        "generated_month": 0,
        "done_month": 0,
        "sold_month": 0,
        "dismissed_month": 0,
        "revenue_month": 0.0,
        "close_rate": 0.0,
        "conversion_rate": 0.0,
    }


def test_unknown_manager_404(client):
    resp = client.get("/head/team", params={"manager_net_uid": UNKNOWN_UID})
    assert resp.status_code == 404
    assert resp.json()["detail"] == "unknown_manager"


def test_head_tasks_non_head_benign(client):
    # Non-head -> benign empty board with is_head=false (200, NOT 403): the console board
    # mounts before the role resolves, and the gba-server proxy mislabels 403 as
    # ai_auth_misconfigured. Same contract as every other /head route.
    resp = client.get("/head/tasks", params={"manager_net_uid": MGR_UID})
    assert resp.status_code == 200
    body = resp.json()
    assert body["is_head"] is False
    assert body["requested_manager_net_uid"] == MGR_UID.lower()
    assert body["returned_count"] == 0
    assert body["by_status"] == {
        "open": 0,
        "in_progress": 0,
        "done": 0,
        "snoozed": 0,
        "dismissed": 0,
    }
    assert body["total"] == 0 and body["tasks"] == [] and body["managers"] == []


def test_head_tasks_unknown_404(client):
    resp = client.get("/head/tasks", params={"manager_net_uid": UNKNOWN_UID})
    assert resp.status_code == 404
    assert resp.json()["detail"] == "unknown_manager"


def test_head_tasks_shape_for_head(client):
    from app.services import lifecycle

    k_crit = _seed(1, "mgr:1|client:10|board:crit", urgency=Urgency.CRITICAL)
    _seed(2, "mgr:2|client:11|board:hi", urgency=Urgency.HIGH)
    k_ip = _seed(1, "mgr:1|client:12|board:ip", urgency=Urgency.HIGH)
    lifecycle.change_status(k_ip, TaskStatus.IN_PROGRESS, by=1)

    resp = client.get("/head/tasks", params={"manager_net_uid": HEAD_UID,
                                             "statuses": "open,in_progress", "limit": 50})
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {
        "is_head",
        "requested_manager_net_uid",
        "requested_statuses",
        "requested_manager_id",
        "requested_urgency",
        "requested_task_type",
        "skip",
        "limit",
        "returned_count",
        "total",
        "tasks",
        "by_status",
        "managers",
    }
    assert body["is_head"] is True
    assert body["requested_manager_net_uid"] == HEAD_UID.lower()
    assert body["requested_statuses"] == ["open", "in_progress"]
    assert body["returned_count"] == 3
    assert body["total"] == 3
    assert body["by_status"] == {"open": 2, "in_progress": 1, "done": 0, "snoozed": 0,
                                 "dismissed": 0}
    # board surfaces critical first, then the in-progress (actively-worked) high
    assert [t["task_key"] for t in body["tasks"][:2]] == [k_crit, k_ip]
    row = body["tasks"][0]
    assert set(row) >= {"task_key", "manager_id", "manager_name", "client_id", "client_name",
                        "task_type", "title", "status", "urgency", "priority", "p_outcome",
                        "expected_value", "ev_score", "in_progress_since", "generated_at",
                        "updated_at", "sla_breached"}
    assert row["manager_name"] == "Manager 1"
    # managers dropdown = all_managers()+names
    assert body["managers"] == [{"manager_id": 1, "name": "Manager 1"},
                                {"manager_id": 2, "name": "Manager 2"}]


def test_head_tasks_manager_filter(client):
    _seed(1, "mgr:1|client:10|f", urgency=Urgency.HIGH)
    _seed(2, "mgr:2|client:11|f", urgency=Urgency.HIGH)
    resp = client.get("/head/tasks", params={"manager_net_uid": HEAD_UID, "manager_id": 2})
    assert resp.status_code == 200
    body = resp.json()
    assert {t["manager_id"] for t in body["tasks"]} == {2}
    assert body["total"] == 1


def test_head_tasks_excludes_stale_tasks_outside_active_manager_scope(client):
    _seed(1, "mgr:1|client:10|active", urgency=Urgency.HIGH)
    _seed(3, "mgr:3|client:11|stale", urgency=Urgency.HIGH)

    response = client.get("/head/tasks", params={"manager_net_uid": HEAD_UID})

    assert response.status_code == 200
    body = response.json()
    assert {task["manager_id"] for task in body["tasks"]} == {1}
    assert body["total"] == 1
    assert {manager["manager_id"] for manager in body["managers"]} == {1, 2}


def test_head_tasks_rejects_filter_outside_active_manager_scope(client):
    response = client.get(
        "/head/tasks",
        params={"manager_net_uid": HEAD_UID, "manager_id": 3},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "manager_not_in_active_team_scope"


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
    as_of = datetime.now(ZoneInfo("Europe/Kyiv")).date().isoformat()
    month_start, _ = lifecycle.business_month_bounds_utc(as_of)
    last_month = month_start - timedelta(seconds=1)
    mongo.tasks().update_one({"task_key": key_old}, {"$set": {"resolved_at": last_month}})

    stats = lifecycle.team_stats(1, as_of)
    assert stats["done_month"] == 1
    assert stats["sold_month"] == 1
    assert stats["revenue_month"] == 5000.0
    assert stats["close_rate"] == 0.5      # done 1 / (done 1 + dismissed 1)
    assert stats["conversion_rate"] == 1.0  # sold 1 / done 1
    assert stats["dismissed_month"] == 1
    assert stats["active"] == 1
    assert key_active  # the open task is the lone active one


def test_head_team_fails_closed_on_revenue_without_a_sold_task(client, monkeypatch):
    from app.api import main

    monkeypatch.setattr(main, "_kyiv_today", lambda: date(2026, 6, 8))
    monkeypatch.setattr(
        main.lifecycle,
        "team_stats",
        lambda manager_id, as_of: {
            "active": 0,
            "generated_month": 0,
            "done_month": 1,
            "sold_month": 0,
            "dismissed_month": 0,
            "revenue_month": 0.01,
            "close_rate": 1.0,
            "conversion_rate": 0.0,
        },
    )

    response = client.get(
        "/head/team",
        params={"manager_net_uid": HEAD_UID, "as_of_date": "2026-06-08"},
    )

    assert response.status_code == 502
    assert response.json()["detail"] == "team_stats_invariant_failed"
