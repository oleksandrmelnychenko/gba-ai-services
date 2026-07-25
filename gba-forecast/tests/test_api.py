from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.api import main


@pytest.fixture(autouse=True)
def _verified_synthetic_product(monkeypatch):
    monkeypatch.setattr(
        main.sig,
        "synthetic_product_status",
        lambda: {
            "product_id": 999,
            "resolved": True,
            "source": "verified_database",
        },
    )


def _history(status: str = "not_requested") -> dict:
    sufficient = status == "sufficient"
    return {
        "status": status,
        "month_count": 3 if sufficient else 0,
        "non_zero_month_count": 3 if sufficient else 0,
        "total_eur": 30.0 if sufficient else 0.0,
        "sufficient": sufficient,
    }


def _cached_client_payload(client_net_id: str, as_of: str, horizon: int) -> dict:
    return {
        "ByClient": [{"SaleAmount": 10.0, "MonthNameUK": f"month-{index}"} for index in range(horizon)],
        "ByProduct": [],
        "ByClientAndProduct": [],
        "meta": {
            "status": "ready",
            "as_of": as_of,
            "requested_as_of": None,
            "horizon_months": horizon,
            "currency": "EUR",
            "model_version": main.settings.model_version,
            "source_fingerprint": "source-epoch",
            "requested": {
                "client_net_id": client_net_id.lower(),
                "product_net_id": None,
            },
            "resolved": {
                "client_id": 123,
                "client_net_id": client_net_id.lower(),
                "product_id": None,
                "product_net_id": None,
            },
            "identity": {"client": "resolved", "product": "not_requested"},
            "history_window_months": main.settings.history_months,
            "minimum_non_zero_months": main.settings.min_history_months,
            "history": {
                "ByClient": _history("sufficient"),
                "ByProduct": _history(),
                "ByClientAndProduct": _history(),
            },
        },
    }


def test_internal_key_guards_forecast_and_metrics(monkeypatch):
    monkeypatch.setattr(main.settings, "internal_api_key", "secret")
    monkeypatch.setattr(main.cache, "get", lambda key: None)
    monkeypatch.setattr(main.cache, "set", lambda key, value: None)

    client = TestClient(main.app)

    assert client.get("/health").status_code == 200
    assert client.get("/metrics").status_code == 401
    assert client.get("/forecast/sales").status_code == 401

    response = client.get("/forecast/sales", headers={"X-Internal-Api-Key": "secret"})

    assert response.status_code == 200
    body = response.json()
    assert body["ByClient"] == body["ByProduct"] == body["ByClientAndProduct"] == []
    assert body["meta"]["status"] == "no_scope"
    assert body["meta"]["currency"] == "EUR"


def test_forecast_rejects_invalid_uuid(monkeypatch):
    monkeypatch.setattr(main.settings, "internal_api_key", "")

    response = TestClient(main.app).get("/forecast/sales?client_net_id=not-a-guid")

    assert response.status_code == 422


def test_business_date_uses_kyiv_not_utc_at_midnight_boundary():
    assert main._today(datetime(2026, 7, 24, 21, 30, tzinfo=UTC)) == "2026-07-25"
    assert main._today(datetime(2026, 1, 1, 22, 30, tzinfo=UTC)) == "2026-01-02"


def test_forecast_rejects_horizon_above_cap(monkeypatch):
    monkeypatch.setattr(main.settings, "internal_api_key", "")
    monkeypatch.setattr(main.settings, "max_forecast_horizon_months", 24)

    response = TestClient(main.app).get("/forecast/sales?months=25")

    assert response.status_code == 422
    assert response.json()["detail"] == "months must be <= 24"


def test_forecast_rejects_historical_as_of_and_echoes_explicit_current_date(monkeypatch):
    monkeypatch.setattr(main.settings, "internal_api_key", "")
    monkeypatch.setattr(main, "_today", lambda: "2026-07-25")
    client = TestClient(main.app)

    historical = client.get("/forecast/sales", params={"as_of_date": "2026-07-24"})

    assert historical.status_code == 422
    assert historical.json()["detail"] == "historical_as_of_not_supported"

    monkeypatch.setattr(main.sig, "synthetic_product_id", lambda: 999)
    monkeypatch.setattr(main.sig, "forecast_source_fingerprint", lambda *args: "no-scope")
    monkeypatch.setattr(main.cache, "get", lambda key: None)
    monkeypatch.setattr(main.cache, "set", lambda key, value: None)
    current = client.get("/forecast/sales", params={"as_of_date": "2026-07-25"})

    assert current.status_code == 200
    assert current.json()["meta"]["requested_as_of"] == "2026-07-25"
    assert current.json()["meta"]["as_of"] == "2026-07-25"


def test_forecast_cache_hit_revalidates_identity_without_history_read(monkeypatch):
    monkeypatch.setattr(main.settings, "internal_api_key", "")
    client_net_id = "7845841E-0678-4364-A346-2CE21C7378AB"
    as_of = "2026-07-25"
    horizon = main.settings.forecast_horizon_months
    monkeypatch.setattr(main, "_today", lambda: as_of)
    monkeypatch.setattr(
        main.cache,
        "get",
        lambda key: _cached_client_payload(client_net_id, as_of, horizon),
    )
    monkeypatch.setattr(
        main.sig,
        "client_id_for_netuid",
        lambda net_uid: 123,
    )
    monkeypatch.setattr(main.sig, "synthetic_product_id", lambda: 999)
    monkeypatch.setattr(main.sig, "forecast_source_fingerprint", lambda *args: "source-epoch")
    monkeypatch.setattr(
        main.sig,
        "monthly_sales_by_client",
        lambda *args: (_ for _ in ()).throw(AssertionError("history must not run on cache hit")),
    )

    response = TestClient(main.app).get(f"/forecast/sales?client_net_id={client_net_id}")

    assert response.status_code == 200
    assert len(response.json()["ByClient"]) == horizon
    assert response.json()["meta"]["resolved"]["client_id"] == 123


def test_use_cache_false_bypasses_read_and_write(monkeypatch):
    monkeypatch.setattr(main.settings, "internal_api_key", "")
    client_net_id = "7845841E-0678-4364-A346-2CE21C7378AB"
    monkeypatch.setattr(main, "_today", lambda: "2026-07-25")
    monkeypatch.setattr(
        main.cache,
        "get",
        lambda key: (_ for _ in ()).throw(AssertionError("cache read must be bypassed")),
    )
    monkeypatch.setattr(
        main.cache,
        "set",
        lambda key, value: (_ for _ in ()).throw(AssertionError("cache write must be bypassed")),
    )
    monkeypatch.setattr(main.sig, "client_id_for_netuid", lambda net_uid: 123)
    monkeypatch.setattr(main.sig, "synthetic_product_id", lambda: 999)
    monkeypatch.setattr(main.sig, "forecast_source_fingerprint", lambda *args: "source-epoch")
    monkeypatch.setattr(main.sig, "monthly_sales_by_client", lambda *args: [])

    response = TestClient(main.app).get(
        "/forecast/sales",
        params={"client_net_id": client_net_id, "use_cache": "false"},
    )

    assert response.status_code == 200
    assert response.json()["meta"]["resolved"]["client_id"] == 123


def test_cache_identity_mismatch_is_rebuilt_from_canonical_source(monkeypatch):
    monkeypatch.setattr(main.settings, "internal_api_key", "")
    client_net_id = "7845841E-0678-4364-A346-2CE21C7378AB"
    as_of = "2026-07-25"
    horizon = main.settings.forecast_horizon_months
    monkeypatch.setattr(main, "_today", lambda: as_of)
    monkeypatch.setattr(
        main.cache,
        "get",
        lambda key: _cached_client_payload(client_net_id, as_of, horizon),
    )
    monkeypatch.setattr(main.cache, "set", lambda key, value: None)
    monkeypatch.setattr(main.sig, "client_id_for_netuid", lambda net_uid: 124)
    monkeypatch.setattr(main.sig, "synthetic_product_id", lambda: 999)
    monkeypatch.setattr(main.sig, "forecast_source_fingerprint", lambda *args: "source-epoch")
    monkeypatch.setattr(main.sig, "monthly_sales_by_client", lambda *args: [])

    response = TestClient(main.app).get(f"/forecast/sales?client_net_id={client_net_id}")

    assert response.status_code == 200
    assert response.json()["ByClient"] == []
    assert response.json()["meta"]["resolved"]["client_id"] == 124
    assert response.json()["meta"]["status"] == "insufficient_history"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("month_count", main.settings.history_months + 1),
        ("non_zero_month_count", 0),
        ("total_eur", 0.0),
    ],
)
def test_cache_with_impossible_history_proof_is_rebuilt(monkeypatch, field, value):
    monkeypatch.setattr(main.settings, "internal_api_key", "")
    client_net_id = "7845841E-0678-4364-A346-2CE21C7378AB"
    as_of = "2026-07-25"
    horizon = main.settings.forecast_horizon_months
    cached = _cached_client_payload(client_net_id, as_of, horizon)
    cached["meta"]["history"]["ByClient"][field] = value
    monkeypatch.setattr(main, "_today", lambda: as_of)
    monkeypatch.setattr(main.cache, "get", lambda key: cached)
    monkeypatch.setattr(main.cache, "set", lambda key, value: None)
    monkeypatch.setattr(main.sig, "client_id_for_netuid", lambda net_uid: 123)
    monkeypatch.setattr(main.sig, "synthetic_product_id", lambda: 999)
    monkeypatch.setattr(main.sig, "forecast_source_fingerprint", lambda *args: "source-epoch")
    monkeypatch.setattr(main.sig, "monthly_sales_by_client", lambda *args: [])

    response = TestClient(main.app).get(f"/forecast/sales?client_net_id={client_net_id}")

    assert response.status_code == 200
    assert response.json()["ByClient"] == []
    assert response.json()["meta"]["history"]["ByClient"]["month_count"] == 0


def test_source_change_during_build_fails_closed_before_cache_write(monkeypatch):
    monkeypatch.setattr(main.settings, "internal_api_key", "")
    monkeypatch.setattr(main, "_today", lambda: "2026-07-25")
    monkeypatch.setattr(main.cache, "get", lambda key: None)
    monkeypatch.setattr(
        main.cache,
        "set",
        lambda key, value: (_ for _ in ()).throw(AssertionError("mixed snapshot must not cache")),
    )
    monkeypatch.setattr(main.sig, "client_id_for_netuid", lambda net_uid: 123)
    monkeypatch.setattr(main.sig, "synthetic_product_id", lambda: 999)
    epochs = iter(["epoch-before", "epoch-after"])
    monkeypatch.setattr(main.sig, "forecast_source_fingerprint", lambda *args: next(epochs))
    monkeypatch.setattr(
        main.sig,
        "monthly_sales_by_client",
        lambda *args: [
            {"ym": "2026-04", "eur": 10},
            {"ym": "2026-05", "eur": 20},
            {"ym": "2026-06", "eur": 30},
        ],
    )

    response = TestClient(main.app).get(
        "/forecast/sales",
        params={"client_net_id": "7845841E-0678-4364-A346-2CE21C7378AB"},
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "sales_source_changed_retry"


def test_unknown_uuid_is_distinguishable_from_insufficient_history(monkeypatch):
    monkeypatch.setattr(main.settings, "internal_api_key", "")
    monkeypatch.setattr(main.cache, "get", lambda key: None)
    monkeypatch.setattr(main.cache, "set", lambda key, value: None)
    monkeypatch.setattr(main.sig, "client_id_for_netuid", lambda net_uid: None)

    net_uid = "00000000-0000-0000-0000-000000000001"
    response = TestClient(main.app).get("/forecast/sales", params={"client_net_id": net_uid})

    assert response.status_code == 200
    meta = response.json()["meta"]
    assert meta["status"] == "unknown_identity"
    assert meta["requested"]["client_net_id"] == net_uid
    assert meta["resolved"]["client_id"] is None
    assert meta["identity"]["client"] == "unknown"
    assert meta["history"]["ByClient"]["status"] == "unknown_identity"


def test_health_is_green_only_with_fresh_canonical_business_data(monkeypatch):
    now = datetime(2026, 7, 25, 12, tzinfo=UTC)
    source = {
        "source_schema_present": True,
        "canonical_row_count": 100,
        "history_row_count": 90,
        "history_product_count": 12,
        "history_client_count": 8,
        "latest_sale_at": now - timedelta(hours=2),
        "invalid_value_row_count": 0,
    }
    monkeypatch.setattr(main, "_database_health", lambda: ("healthy", True))
    monkeypatch.setattr(main.cache, "health", lambda: True)
    monkeypatch.setattr(main.sig, "sales_source_status", lambda as_of, months: source)
    monkeypatch.setattr(main.sig, "synthetic_product_id", lambda: 999)

    healthy = main._health_snapshot(now)

    assert healthy["status"] == "healthy"
    assert healthy["business_ready"] is True
    assert healthy["data"]["source_ready"] is True
    assert healthy["data"]["source_fresh"] is True

    source["latest_sale_at"] = now - timedelta(hours=main.settings.source_max_age_hours + 1)
    stale = main._health_snapshot(now)

    assert stale["status"] == "degraded"
    assert stale["business_ready"] is False
    assert stale["data"]["reason"] == "canonical_source_stale"


def test_ready_returns_503_for_false_green_source(monkeypatch):
    monkeypatch.setattr(
        main,
        "_health_snapshot",
        lambda: {
            "status": "degraded",
            "business_ready": False,
            "db_connected": True,
            "cache_connected": True,
            "data": {"source_ready": False},
        },
    )

    response = main.ready()

    assert response.status_code == 503
