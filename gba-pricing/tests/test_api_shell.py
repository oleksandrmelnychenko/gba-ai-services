"""API shell tests — no DB/Redis; the pricing service is monkeypatched."""
from __future__ import annotations

import sys
import types

from fastapi.testclient import TestClient

from app.api import main
from app.domain.models import (
    Confidence,
    DiscountBand,
    PeerBand,
    PriceRecommendation,
)

CA_UID = "11111111-1111-1111-1111-111111111111"
CA_UID_B = "22222222-2222-2222-2222-222222222222"
CA_UID_C = "33333333-3333-3333-3333-333333333333"


def _headers() -> dict[str, str]:
    if not main.settings.internal_api_key:
        return {}
    return {"X-Internal-Api-Key": main.settings.internal_api_key}


def _fake_reco(product_id: int) -> PriceRecommendation:
    return PriceRecommendation(
        product_id=product_id,
        client_agreement_netuid="ca-uid",
        baseline_price=20.0,
        recommended_price=18.5,
        price_floor=12.0,
        unit_cost_eur=10.0,
        suggested_discount_pct=7.5,
        discount_band=DiscountBand(min_pct=5.0, target_pct=7.5, max_pct=15.0),
        peer_band=PeerBand(p25=17.0, p50=18.5, p75=19.5, n=12),
        confidence=Confidence.HIGH,
        margin_pct_at_recommended=45.95,
        rationale="peer-median",
        source_history_start="2025-01-01",
        requested_start="2025-06-15",
        effective_start="2025-06-15",
        history_complete=True,
        history_fingerprint="history-fingerprint",
        model_fingerprint="model-fingerprint",
    )


def _install_fake_service(monkeypatch):
    mod = types.ModuleType("app.services.pricing.service")

    def recommend_price(product_id=None, **_):
        return _fake_reco(product_id or 1)

    mod.recommend_price = recommend_price
    monkeypatch.setitem(sys.modules, "app.services.pricing.service", mod)


def test_metrics_endpoint():
    client = TestClient(main.app)
    resp = client.get("/metrics", headers=_headers())
    assert resp.status_code == 200
    assert "uptime_seconds" in resp.json()


def test_health_fails_closed_when_business_source_is_not_ready(monkeypatch):
    monkeypatch.setattr(main, "get_engine", lambda: types.SimpleNamespace(
        connect=lambda: _ConnectionContext()
    ))
    monkeypatch.setattr(main.cache, "health", lambda: True)
    monkeypatch.setattr(
        main.repo,
        "source_readiness",
        lambda _max_lag: {
            "business_ready": False,
            "reasons": ["canonical_sales_stale"],
        },
    )
    response = TestClient(main.app).get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "degraded"
    assert response.json()["business_ready"] is False
    assert response.json()["source_history_start"] == "2025-01-01"
    assert response.json()["source"]["effective_start"]
    assert isinstance(response.json()["history_complete"], bool)


def test_ready_requires_database_cache_and_business_source(monkeypatch):
    monkeypatch.setattr(main, "get_engine", lambda: types.SimpleNamespace(
        connect=lambda: _ConnectionContext()
    ))
    monkeypatch.setattr(main.cache, "health", lambda: True)
    monkeypatch.setattr(
        main.repo,
        "source_readiness",
        lambda _max_lag: {"business_ready": True, "reasons": []},
    )
    response = TestClient(main.app).get("/ready")
    assert response.status_code == 200
    assert response.json()["status"] == "ready"
    assert response.json()["source_history_start"] == "2025-01-01"
    assert response.json()["history_fingerprint"]
    assert response.json()["model_fingerprint"]
    assert response.json()["source_history_contract_ready"] is True
    assert response.json()["source"]["source_history_start"] == "2025-01-01"


class _ConnectionContext:
    def __enter__(self):
        return self

    def exec_driver_sql(self, _sql: str) -> None:
        return None

    def __exit__(self, *_args) -> None:
        return None


def test_price_requires_product_identifier():
    client = TestClient(main.app)
    resp = client.post(
        "/price",
        json={"client_agreement_net_uid": CA_UID},
        headers=_headers(),
    )
    assert resp.status_code == 422


def test_price_rejects_malformed_agreement_uid(monkeypatch):
    _install_fake_service(monkeypatch)
    client = TestClient(main.app)
    resp = client.post(
        "/price",
        json={"product_id": 7, "client_agreement_net_uid": "not-a-uuid"},
        headers=_headers(),
    )
    assert resp.status_code == 422


def test_price_rejects_malformed_as_of_date(monkeypatch):
    _install_fake_service(monkeypatch)
    client = TestClient(main.app)
    resp = client.post(
        "/price",
        json={"product_id": 7, "client_agreement_net_uid": CA_UID, "as_of_date": "15-06-2026"},
        headers=_headers(),
    )
    assert resp.status_code == 422


def test_price_accepts_iso_as_of_date(monkeypatch):
    _install_fake_service(monkeypatch)
    client = TestClient(main.app)
    resp = client.post(
        "/price",
        json={"product_id": 7, "client_agreement_net_uid": CA_UID, "as_of_date": "2026-06-15"},
        headers=_headers(),
    )
    assert resp.status_code == 200


def test_price_rejects_as_of_before_source_history_floor(monkeypatch):
    _install_fake_service(monkeypatch)
    client = TestClient(main.app)
    resp = client.post(
        "/price",
        json={"product_id": 7, "client_agreement_net_uid": CA_UID, "as_of_date": "2024-12-31"},
        headers=_headers(),
    )
    assert resp.status_code == 422
    assert resp.json()["detail"] == "as_of_date_before_source_history_start"


def test_price_with_fake_service(monkeypatch):
    _install_fake_service(monkeypatch)
    client = TestClient(main.app)
    resp = client.post(
        "/price",
        json={"product_id": 7, "client_agreement_net_uid": CA_UID},
        headers=_headers(),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["product_id"] == 7
    assert body["currency"] == "EUR"
    assert body["confidence"] == "high"
    assert body["model_version"] == "pricing-ab-v2"
    assert body["source_history_start"] == "2025-01-01"
    assert body["effective_start"] == "2025-06-15"
    assert body["history_complete"] is True
    assert body["history_fingerprint"] == "history-fingerprint"
    assert body["model_fingerprint"] == "model-fingerprint"
    assert body["discount_band"]["target_pct"] == 7.5


def test_price_maps_rejected_synthetic_product_to_not_found(monkeypatch):
    mod = types.ModuleType("app.services.pricing.service")

    def recommend_price(**_):
        raise LookupError("product not found")

    mod.recommend_price = recommend_price
    monkeypatch.setitem(sys.modules, "app.services.pricing.service", mod)

    client = TestClient(main.app)
    resp = client.post(
        "/price",
        json={"product_id": 777, "client_agreement_net_uid": CA_UID},
        headers=_headers(),
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "product not found"


def test_price_rejects_nonpositive_product_id():
    client = TestClient(main.app)
    resp = client.post(
        "/price",
        json={"product_id": 0, "client_agreement_net_uid": CA_UID},
        headers=_headers(),
    )
    assert resp.status_code == 422


def test_price_batch_isolates_errors(monkeypatch):
    mod = types.ModuleType("app.services.pricing.service")

    def recommend_price(product_id=None, **_):
        if product_id == 99:
            raise ValueError("boom")
        return _fake_reco(product_id)

    mod.recommend_price = recommend_price
    monkeypatch.setitem(sys.modules, "app.services.pricing.service", mod)

    client = TestClient(main.app)
    resp = client.post("/price/batch", json={"items": [
        {"product_id": 1, "client_agreement_net_uid": CA_UID},
        {"product_id": 99, "client_agreement_net_uid": CA_UID_B},
        {"product_id": 2, "client_agreement_net_uid": CA_UID_C},
    ]}, headers=_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 2
    assert body["failed"] == 1
    assert body["errors"][0]["product_id"] == 99
    assert "boom" not in body["errors"][0]["error"]


def test_price_batch_reports_malformed_uid(monkeypatch):
    _install_fake_service(monkeypatch)
    client = TestClient(main.app)
    resp = client.post("/price/batch", json={"items": [
        {"product_id": 1, "client_agreement_net_uid": CA_UID},
        {"product_id": 2, "client_agreement_net_uid": "not-a-uuid"},
    ]}, headers=_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 1
    assert body["failed"] == 1
    assert body["errors"][0]["error"] == "malformed client_agreement_net_uid"


def test_price_batch_rejects_shared_as_of_before_source_history_floor(monkeypatch):
    _install_fake_service(monkeypatch)
    client = TestClient(main.app)
    resp = client.post(
        "/price/batch",
        json={
            "items": [{"product_id": 1, "client_agreement_net_uid": CA_UID}],
            "as_of_date": "2024-12-31",
        },
        headers=_headers(),
    )
    assert resp.status_code == 422
    assert resp.json()["detail"] == "as_of_date_before_source_history_start"
