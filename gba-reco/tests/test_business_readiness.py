from __future__ import annotations

from datetime import date

from fastapi.testclient import TestClient

from app.api import main


def _headers() -> dict[str, str]:
    return (
        {"X-Internal-Api-Key": main.settings.internal_api_key}
        if main.settings.internal_api_key
        else {}
    )


def test_health_is_not_green_without_business_sources(monkeypatch):
    class _Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def exec_driver_sql(self, sql):
            return 1

    class _Engine:
        def connect(self):
            return _Connection()

    monkeypatch.setattr(main, "get_engine", lambda: _Engine())
    monkeypatch.setattr(main.cache, "health", lambda: True)
    monkeypatch.setattr(
        main.repo,
        "source_readiness",
        lambda max_lag_days: {
            "business_ready": False,
            "reasons": ["sellable_stock_missing"],
            "latest_sale_at": "2026-07-25T10:00:00",
            "stocked_product_count": 0,
            "sellable_storage_count": 2,
            "synthetic_product_count": 1,
        },
    )

    response = TestClient(main.app).get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "degraded"
    assert response.json()["business_ready"] is False
    assert response.json()["source_history_start"] == "2025-01-01"
    assert response.json()["source_history_contract_ready"] is True


def test_ready_returns_503_when_cache_is_unavailable(monkeypatch):
    monkeypatch.setattr(
        main,
        "health",
        lambda: {
            "status": "degraded",
            "db_connected": True,
            "redis_connected": False,
            "business_ready": True,
        },
    )

    response = TestClient(main.app).get("/ready", headers=_headers())

    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"


def test_source_history_mismatch_fails_closed(monkeypatch):
    class _Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def exec_driver_sql(self, sql):
            return 1

    monkeypatch.setattr(
        main,
        "get_engine",
        lambda: type("_Engine", (), {"connect": lambda self: _Connection()})(),
    )
    monkeypatch.setattr(main.cache, "health", lambda: True)
    monkeypatch.setattr(
        main.repo,
        "source_readiness",
        lambda _max_lag: {"business_ready": True, "reasons": []},
    )
    monkeypatch.setattr(
        main.settings,
        "source_history_start_date",
        date(2025, 2, 1),
    )

    payload = main.health()

    assert payload["status"] == "degraded"
    assert payload["business_ready"] is False
    assert payload["source_history_contract_ready"] is False
    assert "source_history_start_mismatch" in payload["reasons"]


def test_recommend_returns_404_for_unknown_customer(monkeypatch):
    monkeypatch.setattr(main.repo, "client_exists", lambda customer_id: False)

    response = TestClient(main.app).post(
        "/recommend",
        json={"customer_id": 999999999},
        headers=_headers(),
    )

    assert response.status_code == 404


def test_copurchase_rejects_unknown_seed_product(monkeypatch):
    monkeypatch.setattr(main.repo, "client_exists", lambda customer_id: True)
    monkeypatch.setattr(main.repo, "active_product_ids", lambda product_ids: set())

    response = TestClient(main.app).post(
        "/recommend/copurchase",
        json={"customer_id": 1, "product_ids": [444]},
        headers=_headers(),
    )

    assert response.status_code == 404
    assert response.json()["detail"]["product_ids"] == [444]
