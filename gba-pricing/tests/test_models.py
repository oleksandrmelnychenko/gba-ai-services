"""Domain-model contract tests — pure pydantic, no DB/Redis."""
from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.domain.models import (
    BatchPriceRequest,
    Confidence,
    DiscountBand,
    PeerBand,
    PriceRecommendation,
    PriceRequest,
)


def test_recommendation_defaults_and_roundtrip():
    reco = PriceRecommendation(product_id=42, client_agreement_netuid="ca-uid")
    assert reco.currency == "EUR"
    assert reco.model_version == "pricing-ab-v2"
    assert reco.confidence == Confidence.LOW
    assert reco.peer_band.n == 0
    assert reco.baseline_price is None
    dumped = reco.model_dump(mode="json")
    again = PriceRecommendation(**dumped)
    assert again.product_id == 42
    assert again.currency == "EUR"


def test_recommendation_full_payload():
    reco = PriceRecommendation(
        product_id=7,
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
        as_of_date="2026-06-15",
    )
    assert reco.discount_band.max_pct == 15.0
    assert reco.peer_band.p50 == 18.5
    assert reco.confidence == Confidence.HIGH


def test_price_request_culture_and_vat_defaults():
    req = PriceRequest(product_id=1, client_agreement_net_uid="ca-uid")
    assert req.culture == "uk"
    assert req.with_vat is True
    assert req.use_cache is True
    assert req.target_margin_pct is None


def test_batch_request_accepts_items():
    req = BatchPriceRequest(items=[
        {"product_net_uid": "p1", "client_agreement_net_uid": "a"},
        {"product_id": 2, "client_agreement_net_uid": "b"},
    ])
    assert len(req.items) == 2
    assert req.items[0].product_net_uid == "p1"
    assert req.items[1].product_id == 2


def test_request_requires_positive_product_identity():
    with pytest.raises(ValidationError):
        PriceRequest(client_agreement_net_uid="ca-uid")
    with pytest.raises(ValidationError):
        PriceRequest(product_id=0, client_agreement_net_uid="ca-uid")
    with pytest.raises(ValidationError):
        PriceRequest(product_net_uid="   ", client_agreement_net_uid="ca-uid")


def test_response_money_rounds_decimal_half_up_and_rejects_nonfinite():
    reco = PriceRecommendation(
        product_id=7,
        client_agreement_netuid="ca-uid",
        baseline_price=Decimal("1.005"),
        recommended_price=Decimal("2.675"),
        peer_band=PeerBand(
            p25=Decimal("1.005"),
            p50=Decimal("2.675"),
            p75=Decimal("3.335"),
        ),
    )
    assert reco.baseline_price == 1.01
    assert reco.recommended_price == 2.68
    assert reco.peer_band.p25 == 1.01
    assert reco.peer_band.p50 == 2.68
    assert reco.peer_band.p75 == 3.34
    dumped = reco.model_dump(mode="json")
    assert dumped["baseline_price"] == 1.01
    assert isinstance(dumped["baseline_price"], float)

    with pytest.raises(ValidationError):
        PriceRecommendation(
            product_id=7,
            client_agreement_netuid="ca-uid",
            baseline_price=float("nan"),
        )


def test_business_bands_must_be_monotone():
    with pytest.raises(ValidationError):
        DiscountBand(min_pct=10, target_pct=5, max_pct=20)
    with pytest.raises(ValidationError):
        PeerBand(p25=20, p50=10, p75=30)
