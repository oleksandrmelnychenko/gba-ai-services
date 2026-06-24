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


def test_price_requires_product_identifier():
    client = TestClient(main.app)
    resp = client.post(
        "/price",
        json={"client_agreement_net_uid": "ca-uid"},
        headers=_headers(),
    )
    assert resp.status_code == 422


def test_price_with_fake_service(monkeypatch):
    _install_fake_service(monkeypatch)
    client = TestClient(main.app)
    resp = client.post(
        "/price",
        json={"product_id": 7, "client_agreement_net_uid": "ca-uid"},
        headers=_headers(),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["product_id"] == 7
    assert body["currency"] == "EUR"
    assert body["confidence"] == "high"
    assert body["model_version"] == "pricing-ab-v2"
    assert body["discount_band"]["target_pct"] == 7.5


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
        {"product_id": 1, "client_agreement_net_uid": "a"},
        {"product_id": 99, "client_agreement_net_uid": "b"},
        {"product_id": 2, "client_agreement_net_uid": "c"},
    ]}, headers=_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 2
    assert body["failed"] == 1
    assert body["errors"][0]["product_id"] == 99
