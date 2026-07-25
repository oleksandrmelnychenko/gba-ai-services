"""Service-facade tests — the repository is fully MOCKED (no live DB). Covers resolution,
LookupError->404 contract, cache-hit hydration, the no-cost peer-only path, and the synthetic-
line exclusion assumption baked into the repository SQL."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

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
    monkeypatch.setattr(service.repo, "active_group_discount", lambda *a, **k: over.get(
        "active_discount", 0.0))
    monkeypatch.setattr(service.repo, "is_promotional", lambda *a, **k: over.get(
        "promotional", False))
    monkeypatch.setattr(service.repo, "segment_discount_distribution", lambda *a, **k: over.get(
        "segment", {"p75": 12.0, "p90": 18.0, "n": 40}))


def test_recommend_resolves_and_assembles(monkeypatch):
    _wire_repo(monkeypatch)
    out = service.recommend_price(
        product_id=7, product_net_uid=None, client_agreement_net_uid="ca-uid",
        as_of_date="2026-06-15", use_cache=False,
    )
    assert out.product_id == 7
    assert out.product_net_uid == "p-uid"
    assert out.client_agreement_netuid == "ca-uid"
    assert out.baseline_price == 20.0
    assert out.recommended_price == 18.5
    assert out.price_floor == 11.2
    assert out.confidence == Confidence.HIGH
    assert out.rationale == "peer-median"
    assert out.model_version == "pricing-ab-v2"
    assert out.source_history_start == "2025-01-01"
    assert out.requested_start == "2025-06-15"
    assert out.effective_start == "2025-06-15"
    assert out.history_complete is True
    assert out.history_fingerprint
    assert out.model_fingerprint


def test_recommend_live_baseline_reports_agreement_source(monkeypatch):
    _wire_repo(monkeypatch)
    out = service.recommend_price(
        product_id=7, product_net_uid=None, client_agreement_net_uid="ca-uid",
        as_of_date="2026-06-15", use_cache=False,
    )
    assert out.baseline_source == "agreement"


def test_recommend_discloses_partial_history_near_source_floor(monkeypatch):
    _wire_repo(monkeypatch)
    out = service.recommend_price(
        product_id=7,
        product_net_uid=None,
        client_agreement_net_uid="ca-uid",
        as_of_date="2025-06-15",
        use_cache=False,
    )
    assert out.source_history_start == "2025-01-01"
    assert out.requested_start == "2024-06-15"
    assert out.effective_start == "2025-01-01"
    assert out.history_complete is False


def test_recommend_null_baseline_uses_client_world_fallback(monkeypatch):
    _wire_repo(monkeypatch, baseline=None)
    monkeypatch.setattr(
        service.repo, "client_world_fallback_baseline",
        lambda *a, **k: {"fallback_price": 14.55, "n": 5},
    )
    out = service.recommend_price(
        product_id=7, product_net_uid=None, client_agreement_net_uid="ca-uid",
        as_of_date="2026-06-15", use_cache=False,
    )
    assert out.baseline_price == 14.55
    assert out.baseline_source == "client_world_fallback"
    assert out.recommended_price == 14.55
    assert out.rationale != "no-baseline"


def test_recommend_null_baseline_undeterminable_world_stays_no_baseline(monkeypatch):
    _wire_repo(monkeypatch, baseline=None)
    monkeypatch.setattr(
        service.repo, "client_world_fallback_baseline", lambda *a, **k: None
    )
    out = service.recommend_price(
        product_id=7, product_net_uid=None, client_agreement_net_uid="ca-uid",
        as_of_date="2026-06-15", use_cache=False,
    )
    assert out.baseline_price is None
    assert out.baseline_source is None
    assert out.recommended_price is None
    assert out.confidence == Confidence.LOW
    assert out.rationale == "no-baseline"


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


def test_recommend_rejects_out_of_range_business_margin(monkeypatch):
    _wire_repo(monkeypatch)
    with pytest.raises(ValueError, match="target_margin_pct"):
        service.recommend_price(
            product_id=7,
            product_net_uid=None,
            client_agreement_net_uid="ca-uid",
            target_margin_pct=Decimal("100.01"),
            as_of_date="2026-06-15",
            use_cache=False,
        )


def test_recommend_rejects_pre_floor_as_of_before_repository_access(monkeypatch):
    def unexpected_resolve(*_args, **_kwargs):
        raise AssertionError("pre-floor requests must fail before repository access")

    monkeypatch.setattr(service.repo, "resolve_product", unexpected_resolve)
    with pytest.raises(ValueError, match="as_of_date_before_source_history_start"):
        service.recommend_price(
            product_id=7,
            product_net_uid=None,
            client_agreement_net_uid="ca-uid",
            as_of_date="2024-12-31",
            use_cache=False,
        )


def test_marked_up_derivation_stays_decimal(monkeypatch):
    _wire_repo(monkeypatch)
    marked_up = service._marked_up_from_baseline(Decimal("1.005"), Decimal("10"))
    assert isinstance(marked_up, Decimal)
    assert marked_up == Decimal("1.005") / Decimal("0.9")


def test_recommend_cache_hit_hydrates(monkeypatch):
    _wire_repo(monkeypatch)
    from app.domain.models import DiscountBand, PeerBand, PriceRecommendation
    settings = service.get_settings()
    coverage = service._history_fields(
        service.trailing_month_history_window(
            "2026-06-15",
            settings.trailing_window_months,
            settings.source_history_start_date,
        ),
        settings,
    )
    cached_obj = PriceRecommendation(
        product_id=7, client_agreement_netuid="ca-uid", baseline_price=20.0,
        product_net_uid="p-uid",
        recommended_price=18.5, price_floor=11.2, unit_cost_eur=10.0,
        suggested_discount_pct=7.5,
        discount_band=DiscountBand(min_pct=18.0, target_pct=18.0, max_pct=44.0),
        peer_band=PeerBand(p25=17.0, p50=18.5, p75=19.5, n=12),
        confidence=Confidence.HIGH, margin_pct_at_recommended=45.95,
        rationale="peer-median", as_of_date="2026-06-15",
        **coverage,
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


@pytest.mark.parametrize(
    ("lineage_field", "replacement"),
    [
        ("source_history_start", None),
        ("source_history_start", "2024-01-01"),
        ("history_fingerprint", None),
        ("history_fingerprint", "wrong-history"),
    ],
)
def test_recommend_rejects_cache_artifact_without_matching_history_lineage(
    monkeypatch,
    lineage_field,
    replacement,
):
    _wire_repo(monkeypatch)
    fresh = service.recommend_price(
        product_id=7,
        product_net_uid=None,
        client_agreement_net_uid="ca-uid",
        as_of_date="2026-06-15",
        use_cache=False,
    ).model_dump(mode="json")
    if replacement is None:
        fresh.pop(lineage_field)
    else:
        fresh[lineage_field] = replacement
    monkeypatch.setattr(service.cache, "get", lambda *a, **k: fresh)

    calls = {"baseline": 0}

    def baseline(*_args, **_kwargs):
        calls["baseline"] += 1
        return 20.0

    monkeypatch.setattr(service.repo, "baseline_price", baseline)
    service.recommend_price(
        product_id=7,
        product_net_uid=None,
        client_agreement_net_uid="ca-uid",
        as_of_date="2026-06-15",
        use_cache=True,
    )
    assert calls["baseline"] == 1


def test_recommend_rejects_cache_entry_for_different_product_identity(monkeypatch):
    _wire_repo(monkeypatch)
    from app.domain.models import PeerBand, PriceRecommendation

    cached = PriceRecommendation(
        product_id=7,
        product_net_uid="different-product",
        client_agreement_netuid="ca-uid",
        baseline_price=999.0,
        recommended_price=999.0,
        peer_band=PeerBand(),
    ).model_dump(mode="json")
    monkeypatch.setattr(service.cache, "get", lambda *a, **k: cached)

    out = service.recommend_price(
        product_id=7,
        product_net_uid=None,
        client_agreement_net_uid="ca-uid",
        as_of_date="2026-06-15",
        use_cache=True,
    )
    assert out.product_net_uid == "p-uid"
    assert out.recommended_price == 18.5


def test_source_floor_lineage_does_not_change_current_price_or_cent_rounding(monkeypatch):
    _wire_repo(monkeypatch)
    settings = service.get_settings()
    original_floor = settings.source_history_start_date
    first = service.recommend_price(
        product_id=7,
        product_net_uid=None,
        client_agreement_net_uid="ca-uid",
        as_of_date="2026-06-15",
        use_cache=False,
    )
    monkeypatch.setattr(settings, "source_history_start_date", date(2024, 1, 1))
    second = service.recommend_price(
        product_id=7,
        product_net_uid=None,
        client_agreement_net_uid="ca-uid",
        as_of_date="2026-06-15",
        use_cache=False,
    )
    assert original_floor == date(2025, 1, 1)
    assert (
        first.baseline_price,
        first.recommended_price,
        first.price_floor,
        first.unit_cost_eur,
        first.suggested_discount_pct,
    ) == (
        second.baseline_price,
        second.recommended_price,
        second.price_floor,
        second.unit_cost_eur,
        second.suggested_discount_pct,
    )
    assert first.history_fingerprint != second.history_fingerprint
    assert first.model_fingerprint != second.model_fingerprint


def test_synthetic_line_excluded_in_repository_sql():
    import inspect

    from app.data import pricing_repository
    assert ":synthetic" in inspect.getsource(pricing_repository.unit_cost_eur)
    assert ":synthetic" in inspect.getsource(pricing_repository.peer_band)
    assert "IsValidForCurrentSale = 1" in inspect.getsource(pricing_repository.peer_band)
    fallback_src = inspect.getsource(pricing_repository.client_world_fallback_baseline)
    assert ":synthetic" in fallback_src
    assert "IsValidForCurrentSale = 1" in fallback_src
    assert "PriceSourceIsAmg = req.world" in fallback_src
    assert "PriceSourceIsAmg IS NOT NULL" in fallback_src
