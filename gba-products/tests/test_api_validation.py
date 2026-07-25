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


def test_product_routes_reject_non_positive_identity_before_dependencies():
    paths = [
        "/product/0",
        "/product/0/analytics",
        "/product/0/regions",
        "/product/0/substitutes",
    ]

    for path in paths:
        assert client.get(path, headers=_headers).status_code == 422


def test_all_point_in_time_routes_reject_as_of_before_source_floor():
    paths = [
        "/assortment/stock",
        "/assortment/overview",
        "/assortment/health",
        "/assortment/regions",
        "/assortment/margin",
        "/assortment/returns",
        "/product/1",
        "/product/1/analytics",
        "/product/1/regions",
        "/product/1/substitutes",
    ]

    for path in paths:
        response = client.get(
            path,
            params={"as_of_date": "2024-12-31"},
            headers=_headers,
        )
        assert response.status_code == 422
        assert response.json()["detail"] == "as_of_date_before_source_history_start"
