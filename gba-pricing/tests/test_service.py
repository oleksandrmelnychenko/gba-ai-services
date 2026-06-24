"""Service-facade tests — the repository is fully MOCKED (no live DB). Covers resolution,
LookupError->404 contract, cache-hit hydration, the no-cost peer-only path, and the synthetic-
line exclusion assumption baked into the repository SQL."""
from __future__ import annotations

import pytest

from app.domain.models import Confidence
from app.services.pricing import service


@pytest.fixture(autouse=True)
def _no_cache(monkeypatch):
    monkeypatch.setattr(service.cache, "get", lambda *a, **k: None)
    monkeypatch.setattr(service.cache, "set", lambda *a, **k: None)


def _wire_repo(monkeypatch, **over):
    monkeypatch.setattr(service.repo, "resolve_product", lambda pid, uid: over.get(
        "product", {"id": pid or 7, "net_uid": "p-uid"}))
    monkeypatch.setattr(service.repo, "resolve_client_agreement", lambda uid: over.get(
        "agreement", {
            "client_agreement_id": 11, "client_agreement_netuid": uid,
            "agreement_id": 22, "pricing_id": 849, "currency_id": 2}))
    monkeypatch.setattr(service.repo, "baseline_price", lambda *a, **k: over.get("baseline", 20.0))
    monkeypatch.setattr(service.repo, "base_list_price_and_markup", lambda *a, **k: over.get(
        "list_markup", {
            "base_price": 20.0, "extra_charge": 0.0, "base_pricing_id": 849, "culture": "uk"}))
    monkeypatch.setattr(service.repo, "unit_cost_eur", lambda *a, **k: over.get(
        "cost", {"unit_cost_eur": 10.0, "lot_count": 4, "cost_source": "median_onhand"}))
    monkeypatch.setattr(service.repo, "peer_band", lambda *a, **k: over.get(
        "peer", {"p25": 17.0, "p50": 18.5, "p75": 19.5, "n": 12}))
    monkeypatch.setattr(service.repo, "product_group_id", lambda *a, **k: over.get("pg_id", 106))
    monkeypatch.setattr(service.repo, "segment_discount_distribution", lambda *a, **k: over.get(
        "segment", {"p75": 12.0, "p90": 18.0, "n": 40}))


def test_recommend_resolves_and_assembles(monkeypatch):
    _wire_repo(monkeypatch)
    out = service.recommend_price(
        product_id=7, product_net_uid=None, client_agreement_net_uid="ca-uid",
        as_of_date="2026-06-15", use_cache=False,
    )
    assert out.product_id == 7
    assert out.client_agreement_netuid == "ca-uid"
    assert out.baseline_price == 20.0
    assert out.recommended_price == 18.5
    assert out.price_floor == 11.2
    assert out.confidence == Confidence.HIGH
    assert out.rationale == "peer-median"
    assert out.model_version == "pricing-ab-v2"


def test_recommend_unknown_product_raises_lookup(monkeypatch):
    _wire_repo(monkeypatch, product=None)
    monkeypatch.setattr(service.repo, "resolve_product", lambda *a, **k: None)
    with pytest.raises(LookupError):
        service.recommend_price(
            product_id=None, product_net_uid="missing",
            client_agreement_net_uid="ca-uid", use_cache=False,
        )


def test_recommend_unknown_agreement_raises_lookup(monkeypatch):
    _wire_repo(monkeypatch)
    monkeypatch.setattr(service.repo, "resolve_client_agreement", lambda *a, **k: None)
    with pytest.raises(LookupError):
        service.recommend_price(
            product_id=7, product_net_uid=None,
            client_agreement_net_uid="missing", use_cache=False,
        )


def test_recommend_no_cost_peer_only_low_confidence(monkeypatch):
    _wire_repo(monkeypatch, cost={"unit_cost_eur": None, "lot_count": 0, "cost_source": "none"})
    out = service.recommend_price(
        product_id=7, product_net_uid=None, client_agreement_net_uid="ca-uid",
        as_of_date="2026-06-15", use_cache=False,
    )
    assert out.price_floor is None
    assert out.unit_cost_eur is None
    assert out.recommended_price == 18.5
    assert out.margin_pct_at_recommended is None
    assert out.confidence == Confidence.LOW


def test_recommend_skips_segment_when_no_group_or_tier(monkeypatch):
    _wire_repo(monkeypatch, pg_id=None)

    def boom(*a, **k):
        raise AssertionError("segment_discount_distribution must be skipped without a group")
    monkeypatch.setattr(service.repo, "segment_discount_distribution", boom)
    out = service.recommend_price(
        product_id=7, product_net_uid=None, client_agreement_net_uid="ca-uid",
        as_of_date="2026-06-15", use_cache=False,
    )
    assert out.discount_band.min_pct == 0.0
    assert out.discount_band.max_pct == 44.0
    assert (
        out.discount_band.min_pct
        <= out.discount_band.target_pct
        <= out.discount_band.max_pct
    )


def test_recommend_target_margin_override(monkeypatch):
    _wire_repo(monkeypatch)
    out = service.recommend_price(
        product_id=7, product_net_uid=None, client_agreement_net_uid="ca-uid",
        target_margin_pct=50.0, as_of_date="2026-06-15", use_cache=False,
    )
    assert out.price_floor == 15.0


def test_recommend_cache_hit_hydrates(monkeypatch):
    _wire_repo(monkeypatch)
    from app.domain.models import DiscountBand, PeerBand, PriceRecommendation
    cached_obj = PriceRecommendation(
        product_id=7, client_agreement_netuid="ca-uid", baseline_price=20.0,
        recommended_price=18.5, price_floor=11.2, unit_cost_eur=10.0,
        suggested_discount_pct=7.5,
        discount_band=DiscountBand(min_pct=18.0, target_pct=18.0, max_pct=44.0),
        peer_band=PeerBand(p25=17.0, p50=18.5, p75=19.5, n=12),
        confidence=Confidence.HIGH, margin_pct_at_recommended=45.95,
        rationale="peer-median", as_of_date="2026-06-15",
    ).model_dump(mode="json")
    monkeypatch.setattr(service.cache, "get", lambda *a, **k: cached_obj)

    def boom(*a, **k):
        raise AssertionError("should not recompute on cache hit")
    monkeypatch.setattr(service.repo, "baseline_price", boom)

    out = service.recommend_price(
        product_id=7, product_net_uid=None, client_agreement_net_uid="ca-uid",
        as_of_date="2026-06-15", use_cache=True,
    )
    assert out.recommended_price == 18.5
    assert out.confidence == Confidence.HIGH
    assert out.discount_band.max_pct == 44.0


def test_synthetic_line_excluded_in_repository_sql():
    import inspect

    from app.data import pricing_repository
    assert ":synthetic" in inspect.getsource(pricing_repository.unit_cost_eur)
    assert ":synthetic" in inspect.getsource(pricing_repository.peer_band)
    assert "IsValidForCurrentSale = 1" in inspect.getsource(pricing_repository.peer_band)
