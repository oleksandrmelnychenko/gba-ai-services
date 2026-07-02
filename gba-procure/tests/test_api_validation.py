"""API input-validation tests — no DB (422 raised before the handler runs)."""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.api.main import app
from app.core.config import get_settings

_HEADERS = {"X-Internal-Api-Key": get_settings().internal_api_key}
client = TestClient(app)


def test_plan_producer_malformed_date_422():
    r = client.post("/plan/producer", headers=_HEADERS,
                    json={"producer_id": 1, "as_of_date": "not-a-date"})
    assert r.status_code == 422


def test_cart_plan_unknown_method_422():
    r = client.post("/plan/cart", headers=_HEADERS, json={"method": "banana"})
    assert r.status_code == 422


def test_cart_plan_negative_budget_422():
    assert client.post("/plan/cart", headers=_HEADERS,
                       json={"budget_eur": -5}).status_code == 422


def test_cart_plan_budget_model_rules():
    import pytest
    from pydantic import ValidationError

    from app.api.main import CartPlanRequest

    CartPlanRequest(budget_eur=0)
    CartPlanRequest(budget_eur=1500)
    CartPlanRequest(budget_eur=None)
    with pytest.raises(ValidationError):
        CartPlanRequest(budget_eur=-5)


def test_charts_malformed_date_422():
    r = client.post("/plan/charts", headers=_HEADERS,
                    json={"as_of_date": "2026-13-99"})
    assert r.status_code == 422
