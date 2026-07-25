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


def test_recommend_request_accepts_exact_source_history_boundary():
    from datetime import date

    assert RecommendRequest(customer_id=1, as_of_date="2025-01-01").as_of_date == date(
        2025, 1, 1
    )


@pytest.mark.parametrize(
    "request_factory",
    [
        lambda: RecommendRequest(customer_id=1, as_of_date="2024-12-31"),
        lambda: BatchRequest(customer_ids=[1], as_of_date="2024-12-31"),
    ],
)
def test_requests_reject_dates_before_source_history(request_factory):
    with pytest.raises(ValidationError, match="source history start 2025-01-01"):
        request_factory()


def test_recommend_request_rejects_malformed_date():
    with pytest.raises(ValidationError):
        RecommendRequest(customer_id=1, as_of_date="not-a-date")


def test_batch_request_rejects_malformed_date():
    with pytest.raises(ValidationError):
        BatchRequest(customer_ids=[1], as_of_date="2026-13-99")


@pytest.mark.parametrize(
    "payload",
    [
        {"customer_id": 0},
        {"customer_id": -1},
        {"customer_id": 1, "product_ids": [0]},
        {"customer_id": 1, "product_ids": [7, 7]},
    ],
)
def test_recommend_request_rejects_unsafe_or_duplicate_ids(payload):
    with pytest.raises(ValidationError):
        RecommendRequest(**payload)


def test_batch_request_rejects_duplicate_customer_ids():
    with pytest.raises(ValidationError):
        BatchRequest(customer_ids=[1, 1])


def test_recommend_endpoint_returns_422_for_malformed_date():
    client = TestClient(app)
    resp = client.post(
        "/recommend",
        json={"customer_id": 123, "as_of_date": "garbage"},
        headers=_auth_headers(),
    )
    assert resp.status_code == 422


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("/recommend", {"customer_id": 123, "as_of_date": "2024-12-31"}),
        ("/recommend/copurchase", {"customer_id": 123, "as_of_date": "2024-12-31"}),
        ("/recommend/batch", {"customer_ids": [123], "as_of_date": "2024-12-31"}),
    ],
)
def test_endpoints_return_422_before_source_history_floor(path, payload):
    response = TestClient(app).post(path, json=payload, headers=_auth_headers())

    assert response.status_code == 422
    assert "source history start 2025-01-01" in response.text
