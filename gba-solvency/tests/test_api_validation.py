"""Input-validation tests — no DB/Redis; malformed input is rejected before it reaches SQL."""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.api import main
from app.domain.models import ClientIdentityMismatchError


def _headers() -> dict[str, str]:
    if not main.settings.internal_api_key:
        return {}
    return {"X-Internal-Api-Key": main.settings.internal_api_key}


def test_score_malformed_net_uid_returns_422():
    client = TestClient(main.app)
    resp = client.post(
        "/score", json={"client_net_uid": "not-a-guid"}, headers=_headers()
    )
    assert resp.status_code == 422


def test_score_malformed_as_of_date_returns_422():
    client = TestClient(main.app)
    resp = client.post(
        "/score", json={"client_id": 7, "as_of_date": "garbage"}, headers=_headers()
    )
    assert resp.status_code == 422


def test_score_as_of_before_source_history_returns_422_without_db():
    client = TestClient(main.app)
    resp = client.post(
        "/score",
        json={"client_id": 7, "as_of_date": "2024-12-31"},
        headers=_headers(),
    )
    assert resp.status_code == 422
    assert "2025-01-01" in resp.json()["detail"]


def test_score_requires_positive_client_id():
    client = TestClient(main.app)
    resp = client.post("/score", json={"client_id": 0}, headers=_headers())
    assert resp.status_code == 422


def test_score_dual_identity_mismatch_returns_422(monkeypatch):
    class MismatchedService:
        @staticmethod
        def score_client(**_kwargs):
            raise ClientIdentityMismatchError("client_id and client_net_uid do not match")

    monkeypatch.setattr(main, "_service", lambda: MismatchedService)
    client = TestClient(main.app)
    resp = client.post(
        "/score",
        json={
            "client_id": 7,
            "client_net_uid": "11111111-1111-1111-1111-111111111111",
        },
        headers=_headers(),
    )
    assert resp.status_code == 422
    assert resp.json()["detail"] == "client_id and client_net_uid do not match"


def test_batch_malformed_as_of_date_returns_422():
    client = TestClient(main.app)
    resp = client.post(
        "/score/batch",
        json={"client_ids": [1, 2], "as_of_date": "2026-13-99"},
        headers=_headers(),
    )
    assert resp.status_code == 422


def test_batch_as_of_before_source_history_returns_422_without_db():
    client = TestClient(main.app)
    resp = client.post(
        "/score/batch",
        json={"client_ids": [1, 2], "as_of_date": "2024-12-31"},
        headers=_headers(),
    )
    assert resp.status_code == 422
    assert "2025-01-01" in resp.json()["detail"]


def test_charts_months_out_of_range_returns_422():
    client = TestClient(main.app)
    for months in (0, -5, 61):
        resp = client.get(f"/charts/123?months={months}", headers=_headers())
        assert resp.status_code == 422, months


def test_charts_malformed_as_of_date_returns_422():
    client = TestClient(main.app)
    resp = client.get("/charts/123?as_of_date=garbage", headers=_headers())
    assert resp.status_code == 422


def test_charts_as_of_before_source_history_returns_422_without_db():
    client = TestClient(main.app)
    resp = client.get(
        "/charts/123?as_of_date=2024-12-31",
        headers=_headers(),
    )
    assert resp.status_code == 422
    assert "2025-01-01" in resp.json()["detail"]
