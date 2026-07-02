from fastapi.testclient import TestClient

from app.api.main import app, settings

client = TestClient(app)
_headers = {"X-Internal-Api-Key": settings.internal_api_key} if settings.internal_api_key else {}


def test_malformed_as_of_date_returns_422():
    resp = client.get("/assortment/health", params={"as_of_date": "not-a-date"}, headers=_headers)
    assert resp.status_code == 422


def test_unknown_band_returns_422():
    resp = client.get("/assortment/health", params={"band": "bogus"}, headers=_headers)
    assert resp.status_code == 422


def test_unknown_abc_returns_422():
    resp = client.get("/assortment/health", params={"abc": "bogus"}, headers=_headers)
    assert resp.status_code == 422
