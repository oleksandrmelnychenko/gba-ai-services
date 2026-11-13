"""Unverified invoice dates degrade the target figures explicitly instead of failing the endpoints."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import mongomock
import pytest
from fastapi.testclient import TestClient

from app.data import signals_repository as sig
from app.services import targets

HEAD_UID = "10101010-1010-1010-1010-101010101010"


def _series(months: dict, unverified: int = 0, amount: str = "0") -> sig.InvoiceSeries:
    series = sig.InvoiceSeries(months)
    series.unverified_orders = unverified
    series.unverified_amount = Decimal(amount)
    return series


def test_monthly_shipped_excludes_and_reports_unverified_orders(monkeypatch):
    monkeypatch.setattr(sig, "_excluded", lambda day: [])
    monkeypatch.setattr(sig, "query", lambda sql, params: [
        {"ym": "2026-10", "amt": Decimal("800"), "orders": 8},
        {"ym": None, "amt": Decimal("120.50"), "orders": 2},
    ])
    series = sig.monthly_shipped(1, "2026-08-01", "2026-11-13")
    assert series == {"2026-10": Decimal("800")}
    assert series.unverified_orders == 2
    assert series.unverified_amount == Decimal("120.50")


def test_compute_target_reports_degraded_state_from_verified_orders(monkeypatch):
    months = {"2026-08": 300, "2026-09": 300, "2026-10": 300, "2026-11": 50}
    monkeypatch.setattr(sig, "monthly_shipped", lambda mid, since, asof: _series(months, 3, "75.5"))
    monkeypatch.setattr(sig, "monthly_paid", lambda mid, since, asof: months)
    out = targets.compute_target(7, "2026-11-12")
    assert out["degraded"] is True
    assert out["degraded_reasons"] == ["invoice_event_date_unverified"]
    assert out["unverified_orders"] == 3
    assert out["unverified_amount"] == 75.5
    assert out["shipped"]["target"] == 300.0
    assert out["shipped"]["mtd"] == 50.0


def test_compute_target_is_not_degraded_for_fully_verified_or_legacy_plain_series(monkeypatch):
    months = {"2026-10": 300}
    monkeypatch.setattr(sig, "monthly_shipped", lambda mid, since, asof: months)
    monkeypatch.setattr(sig, "monthly_paid", lambda mid, since, asof: months)
    out = targets.compute_target(7, "2026-11-12")
    assert out["degraded"] is False
    assert out["degraded_reasons"] == []
    assert out["unverified_orders"] == 0


@pytest.fixture
def api(monkeypatch):
    mongo_client = mongomock.MongoClient()
    db = mongo_client["gba_nba_test"]
    from app.data import mongo as m
    monkeypatch.setattr(m, "get_client", lambda: mongo_client)
    monkeypatch.setattr(m, "get_db", lambda: db)
    monkeypatch.setattr(m, "tasks", lambda: db["tasks"])
    monkeypatch.setattr(m, "task_events", lambda: db["task_events"])
    monkeypatch.setattr(m, "manager_prefs", lambda: db["manager_prefs"])
    monkeypatch.setattr(m, "generation_runs", lambda: db["generation_runs"])
    monkeypatch.setattr(m, "ping", lambda: True)

    from app.api import main
    monkeypatch.setattr(main, "_kyiv_today", lambda: date(2026, 11, 12))
    monkeypatch.setattr(main, "_team_snap_state", {"at": 0.0, "as_of": None, "value": None})
    monkeypatch.setattr(main, "_team_metrics_state",
                        {"checked_at": None, "error": None, "unverified_orders": None})
    monkeypatch.setattr(main.signals_repository, "manager_id_for_netuid", lambda nu: 99)
    monkeypatch.setattr(main.signals_repository, "is_head_of_sales", lambda nu: True)
    monkeypatch.setattr(main.signals_repository, "all_managers", lambda: [1, 2])
    monkeypatch.setattr(main.signals_repository, "manager_names",
                        lambda ids: {i: f"Manager {i}" for i in ids})
    months = {"2026-08": 300, "2026-09": 300, "2026-10": 300, "2026-11": 50}
    monkeypatch.setattr(
        sig, "monthly_shipped",
        lambda mid, since, asof: _series(months, 2 if mid == 1 else 0, "10"))
    monkeypatch.setattr(sig, "monthly_paid", lambda mid, since, asof: months)
    return TestClient(main.app), main


def test_three_endpoints_answer_200_with_degraded_note_instead_of_500(api):
    client, _ = api
    target = client.get("/cockpit/target", params={"manager_net_uid": HEAD_UID})
    assert target.status_code == 200
    assert target.json()["degraded"] is False
    assert target.json()["unverified_orders"] == 0

    team = client.get("/head/team", params={"manager_net_uid": HEAD_UID})
    assert team.status_code == 200
    body = team.json()
    assert body["degraded"] is True
    assert body["unverified_orders"] == 2
    assert body["degraded_reasons"] == ["invoice_event_date_unverified"]
    assert [row["unverified_orders"] for row in body["team"]] == [2, 0]
    assert body["totals"]["shipped_mtd"] == 100.0

    overview = client.get("/targets/overview", params={"manager_net_uid": HEAD_UID})
    assert overview.status_code == 200
    assert overview.json()["degraded"] is True
    assert overview.json()["managers"][0]["unverified_orders"] == 2


def test_health_reports_degraded_metrics_and_ready_stays_on_hard_dependencies(api, monkeypatch):
    client, main = api
    assert main._team_metrics_health()["metrics_degraded"] is False
    client.get("/head/team", params={"manager_net_uid": HEAD_UID})
    state = main._team_metrics_health()
    assert state["metrics_degraded"] is True
    assert state["metrics_degraded_reasons"] == ["invoice_event_date_unverified"]
    assert state["unverified_invoice_orders"] == 2

    monkeypatch.setattr(main, "health", lambda: {
        "status": "degraded", "db_connected": True, "mongo_connected": True,
        "business_ready": True, **state})
    assert client.get("/ready").status_code == 200


def test_health_reports_a_failed_team_snapshot(api, monkeypatch):
    client, main = api

    def broken(mid, since, asof):
        raise ValueError("payment_eur_conversion_missing")

    monkeypatch.setattr(sig, "monthly_paid", broken)
    with pytest.raises(ValueError):
        client.get("/head/team", params={"manager_net_uid": HEAD_UID})
    state = main._team_metrics_health()
    assert state["metrics_degraded"] is True
    assert state["metrics_degraded_reasons"] == ["team_targets_failed"]
    assert state["metrics_error"] == "ValueError"
