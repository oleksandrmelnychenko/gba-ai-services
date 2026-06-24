"""API boundary tests for gba-procure.

These keep dangerous payload values from reaching policy/SQL/Mongo, while avoiding DB/Redis by
monkeypatching the service boundary.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.api import main
from app.domain.models import CartReplenishmentPlan


def _headers() -> dict[str, str]:
    if not main.settings.internal_api_key:
        return {}
    return {"X-Internal-Api-Key": main.settings.internal_api_key}


def test_cart_payload_rejects_dangerous_values():
    client = TestClient(main.app)
    bad_payloads = [
        {"limit": -1},
        {"limit": 1001},
        {"budget_eur": -5},
        {"budget_eur": 0},
        {"method": "abc"},
        {"active_days": 0},
        {"as_of_date": "2026-02-30"},
        {"as_of_date": "2026-06-17T23:59:59"},
    ]

    for payload in bad_payloads:
        resp = client.post("/plan/cart", json=payload, headers=_headers())
        assert resp.status_code == 422, payload


def test_cart_payload_normalizes_method_and_date(monkeypatch):
    captured = {}
    monkeypatch.setattr(main.cache, "make_key", lambda *a, **k: "cache-key")
    monkeypatch.setattr(main.cache, "get", lambda *a, **k: None)
    monkeypatch.setattr(main.cache, "set", lambda *a, **k: None)

    def build_cart_plan(
        as_of, only_needed=True, limit=200, budget_eur=None, method="greedy", active_days=None
    ):
        captured.update(
            {
                "as_of": as_of,
                "only_needed": only_needed,
                "limit": limit,
                "budget_eur": budget_eur,
                "method": method,
                "active_days": active_days,
            }
        )
        return CartReplenishmentPlan(items=[], item_count=0, as_of_date=as_of)

    monkeypatch.setattr(main.policy, "build_cart_plan", build_cart_plan)

    client = TestClient(main.app)
    resp = client.post(
        "/plan/cart",
        json={
            "as_of_date": "2026-06-17",
            "limit": 5,
            "budget_eur": 100.0,
            "method": "MILP",
            "active_days": 30,
        },
        headers=_headers(),
    )

    assert resp.status_code == 200
    assert captured == {
        "as_of": "2026-06-17",
        "only_needed": True,
        "limit": 5,
        "budget_eur": 100.0,
        "method": "milp",
        "active_days": 30,
    }


def test_masters_and_feedback_payload_validation():
    client = TestClient(main.app)

    assert client.get("/masters/producer?producer_id=0", headers=_headers()).status_code == 422
    assert (
        client.post(
            "/masters/product-terms",
            json={"producer_id": 1, "product_id": 2, "moq": 0},
            headers=_headers(),
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/feedback",
            json={
                "producer_id": 1,
                "product_id": 2,
                "suggested_qty": 10,
                "final_qty": -1,
                "action": "accept",
            },
            headers=_headers(),
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/feedback",
            json={
                "producer_id": 1,
                "product_id": 2,
                "suggested_qty": 10,
                "final_qty": 10,
                "action": "invalid",
            },
            headers=_headers(),
        ).status_code
        == 422
    )


def test_feedback_normalizes_action_and_abc(monkeypatch):
    captured = {}

    def record(producer_id, product_id, suggested_qty, final_qty, action, abc, at):
        captured.update(
            {
                "producer_id": producer_id,
                "product_id": product_id,
                "suggested_qty": suggested_qty,
                "final_qty": final_qty,
                "action": action,
                "abc": abc,
                "at": at,
            }
        )
        return dict(captured)

    monkeypatch.setattr(main.feedback, "record", record)
    monkeypatch.setattr(main, "_today", lambda: "2026-06-24")

    client = TestClient(main.app)
    resp = client.post(
        "/feedback",
        json={
            "producer_id": 1,
            "product_id": 2,
            "suggested_qty": 10,
            "final_qty": 12,
            "action": "EDIT",
            "abc": "b",
        },
        headers=_headers(),
    )

    assert resp.status_code == 200
    assert captured == {
        "producer_id": 1,
        "product_id": 2,
        "suggested_qty": 10.0,
        "final_qty": 12.0,
        "action": "edit",
        "abc": "B",
        "at": "2026-06-24",
    }


def test_startup_bootstraps_mongo_indexes_when_configured(monkeypatch):
    called = []
    monkeypatch.setattr(main.settings, "mongo_uri", "mongodb://mongo:27017")
    monkeypatch.setattr(main.settings, "use_masters", True)
    monkeypatch.setattr(main.settings, "use_feedback", True)
    monkeypatch.setattr(main.masters, "ensure_indexes", lambda: called.append("masters"))
    monkeypatch.setattr(main.feedback, "ensure_indexes", lambda: called.append("feedback"))

    main._ensure_document_store_indexes()

    assert called == ["masters", "feedback"]
