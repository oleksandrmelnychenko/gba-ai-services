"""Input-validation tests — no DB/Redis; malformed input is rejected before it reaches SQL."""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.api import main


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


def test_batch_malformed_as_of_date_returns_422():
    client = TestClient(main.app)
    resp = client.post(
        "/score/batch",
        json={"client_ids": [1, 2], "as_of_date": "2026-13-99"},
        headers=_headers(),
    )
    assert resp.status_code == 422


def test_charts_months_out_of_range_returns_422():
    client = TestClient(main.app)
    for months in (0, -5, 61):
        resp = client.get(f"/charts/123?months={months}", headers=_headers())
        assert resp.status_code == 422, months


def test_charts_malformed_as_of_date_returns_422():
    client = TestClient(main.app)
    resp = client.get("/charts/123?as_of_date=garbage", headers=_headers())
    assert resp.status_code == 422
