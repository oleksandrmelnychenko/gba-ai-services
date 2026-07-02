"""Request-validation guards — no DB/Redis required.

as_of_date is a typed date on the request models, so malformed values are rejected at
validation (422) instead of reaching SQL and surfacing a 500. Well-formed ISO dates and
omission (the gba-server contract) must still validate.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.main import BatchRequest, RecommendRequest, app
from app.core.config import get_settings


def _auth_headers() -> dict[str, str]:
    key = get_settings().internal_api_key
    return {"X-Internal-Api-Key": key} if key else {}


def test_recommend_request_accepts_iso_date_and_omission():
    from datetime import date

    assert RecommendRequest(customer_id=1).as_of_date is None
    assert RecommendRequest(customer_id=1, as_of_date="2026-06-15").as_of_date == date(2026, 6, 15)


def test_recommend_request_rejects_malformed_date():
    with pytest.raises(ValidationError):
        RecommendRequest(customer_id=1, as_of_date="not-a-date")


def test_batch_request_rejects_malformed_date():
    with pytest.raises(ValidationError):
        BatchRequest(customer_ids=[1], as_of_date="2026-13-99")


def test_recommend_endpoint_returns_422_for_malformed_date():
    client = TestClient(app)
    resp = client.post(
        "/recommend",
        json={"customer_id": 123, "as_of_date": "garbage"},
        headers=_auth_headers(),
    )
    assert resp.status_code == 422
