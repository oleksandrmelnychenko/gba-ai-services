from __future__ import annotations

from fastapi.testclient import TestClient

from app.api import main


def test_internal_key_guards_forecast_and_metrics(monkeypatch):
    monkeypatch.setattr(main.settings, "internal_api_key", "secret")

    client = TestClient(main.app)

    assert client.get("/health").status_code == 200
    assert client.get("/metrics").status_code == 401
    assert client.get("/forecast/sales").status_code == 401

    response = client.get("/forecast/sales", headers={"X-Internal-Api-Key": "secret"})

    assert response.status_code == 200
    assert response.json() == {"ByClient": [], "ByProduct": [], "ByClientAndProduct": []}


def test_forecast_rejects_invalid_uuid(monkeypatch):
    monkeypatch.setattr(main.settings, "internal_api_key", "")

    response = TestClient(main.app).get("/forecast/sales?client_net_id=not-a-guid")

    assert response.status_code == 422


def test_forecast_rejects_horizon_above_cap(monkeypatch):
    monkeypatch.setattr(main.settings, "internal_api_key", "")
    monkeypatch.setattr(main.settings, "max_forecast_horizon_months", 24)

    response = TestClient(main.app).get("/forecast/sales?months=25")

    assert response.status_code == 422
    assert response.json()["detail"] == "months must be <= 24"


def test_forecast_returns_cache_hit_without_db(monkeypatch):
    monkeypatch.setattr(main.settings, "internal_api_key", "")
    monkeypatch.setattr(
        main.cache,
        "get",
        lambda key: {
            "ByClient": [{"SaleAmount": 10.0, "MonthNameUK": "Лип 2026"}],
            "ByProduct": [],
            "ByClientAndProduct": [],
        },
    )
    monkeypatch.setattr(
        main.sig,
        "client_id_for_netuid",
        lambda net_uid: (_ for _ in ()).throw(AssertionError("db lookup must not run on cache hit")),
    )

    response = TestClient(main.app).get("/forecast/sales?client_net_id=7845841E-0678-4364-A346-2CE21C7378AB")

    assert response.status_code == 200
    assert response.json()["ByClient"] == [{"SaleAmount": 10.0, "MonthNameUK": "Лип 2026"}]
