"""Service-facade tests — the repository is fully MOCKED (no live DB). Covers resolution,
LookupError->404 contract, cache-hit hydration, the no-cost peer-only path, and the synthetic-
line exclusion assumption baked into the repository SQL."""
from __future__ import annotations

from datetime import date, datetime
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
    assert out.floor_markup_pct == 12.0
    assert out.floor_margin_pct == 10.71
    assert out.floor_basis == "markup_on_cost"
    assert out.confidence == Confidence.HIGH
    assert out.rationale == "peer-median"
    assert out.model_version == service.get_settings().model_version
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
        discount_band=DiscountBand(min_pct=0.0, target_pct=7.5, max_pct=12.0),
        peer_band=PeerBand(p25=17.0, p50=18.5, p75=19.5, n=12),
        confidence=Confidence.HIGH, margin_pct_at_recommended=45.95,
        rationale="peer-median", as_of_date="2026-06-15",
        **coverage,
        **service.engine.floor_terms(settings.target_margin_pct),
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
    assert out.discount_band.max_pct == 12.0


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
    assert "{KEPT_LINE}" in inspect.getsource(pricing_repository.peer_band)
    assert "IsValidForCurrentSale = 1" in pricing_repository.KEPT_LINE
    fallback_src = inspect.getsource(pricing_repository.client_world_fallback_baseline)
    assert ":synthetic" in fallback_src
    assert "{KEPT_LINE}" in fallback_src
    assert "PriceSourceIsAmg = req.world" in fallback_src
    assert "PriceSourceIsAmg IS NOT NULL" in fallback_src


def test_hydrate_round_trips_conflict_fields_and_discount_base():
    reco = service.engine.build_recommendation(
        product_id=7,
        client_agreement_netuid="33333333-3333-3333-3333-333333333333",
        baseline=Decimal("3.1616"),
        marked_up=Decimal("3.952"),
        cost={"unit_cost_eur": Decimal("2.10"), "lot_count": 4},
        peer={"p25": 3.0, "p50": 3.5, "p75": 3.8, "n": 12},
        segment={"p75": 16, "p90": 18},
        target_margin_pct=12.0,
        as_of_date="2026-06-15",
    ).model_copy(update={"product_net_uid": "44444444-4444-4444-4444-444444444444"})
    hydrated = service._hydrate(
        reco.model_dump(mode="json"),
        expected_product_id=7,
        expected_product_net_uid="44444444-4444-4444-4444-444444444444",
        expected_agreement_net_uid="33333333-3333-3333-3333-333333333333",
        expected_history={},
    )
    assert hydrated is not None
    assert hydrated.model_dump(mode="json") == reco.model_dump(mode="json")
    assert hydrated.constraints_conflict is True
    assert hydrated.discount_base_price == reco.discount_base_price


def test_group_discount_and_cap_use_the_group_of_the_agreement_world(monkeypatch):
    _wire_repo(monkeypatch)
    asked = {}

    def group_for(product_id, agreement_id=None):
        asked["group"] = (product_id, agreement_id)
        return 4381

    def discount_for(client_agreement_id, group_id):
        asked["discount"] = (client_agreement_id, group_id)
        return 0.0

    def segment_for(group_id, base_pricing_id, culture):
        asked["segment"] = (group_id, base_pricing_id, culture)
        return {"p75": 12.0, "p90": 18.0, "n": 40}

    monkeypatch.setattr(service.repo, "product_group_id", group_for)
    monkeypatch.setattr(service.repo, "active_group_discount", discount_for)
    monkeypatch.setattr(service.repo, "segment_discount_distribution", segment_for)
    service.recommend_price(
        product_id=7, product_net_uid=None, client_agreement_net_uid="ca-uid",
        as_of_date="2026-06-15", use_cache=False,
    )
    assert asked["group"] == (7, 22)
    assert asked["discount"] == (11, 4381)
    assert asked["segment"] == (4381, 849, "uk")


def test_with_vat_flag_changes_neither_result_nor_cache_key(monkeypatch):
    _wire_repo(monkeypatch)
    keys: list[str] = []
    engine_flags: list[bool] = []
    monkeypatch.setattr(
        service.cache, "set", lambda key, value, ttl=None: keys.append(key)
    )
    monkeypatch.setattr(
        service.repo,
        "baseline_price",
        lambda product, agreement, culture, with_vat: engine_flags.append(with_vat) or 20.0,
    )
    results = [
        service.recommend_price(
            product_id=7, product_net_uid=None, client_agreement_net_uid="ca-uid",
            as_of_date="2026-06-15", with_vat=flag, use_cache=True,
        )
        for flag in (True, False)
    ]
    assert results[0].model_dump() == results[1].model_dump()
    assert keys[0] == keys[1]
    assert engine_flags == [True, True]


def _synthetic_fallback_query(lines, agreements):
    def fake_query(sql, params=None):
        if "GetCalculatedProductPriceWithSharesAndVat" in sql:
            return [{"baseline_price": None}]
        assert "PriceSourceIsAmg = req.world" in sql
        requested = agreements.get(params["caid"])
        if requested is None or requested["world"] is None:
            return []
        prices = sorted(
            Decimal(str(line["price"]))
            for line in lines
            if line["client_id"] == requested["client_id"]
            and line["product_id"] == params["pid"]
            and line["product_id"] != params["synthetic"]
            and line["world"] == requested["world"]
            and line["price"] > 0
            and params["window_from"] <= line["invoiced"] < params["window_to"]
        )
        if not prices:
            return []
        middle = len(prices) // 2
        median = (
            prices[middle]
            if len(prices) % 2
            else (prices[middle - 1] + prices[middle]) / 2
        )
        return [{"fallback_price": median, "n": len(prices)}]

    return fake_query


_FALLBACK_AGREEMENTS = {
    11: {"client_id": 1, "world": 0},
    12: {"client_id": 1, "world": None},
    13: {"client_id": 2, "world": 1},
}
_FALLBACK_LINES = [
    {"client_id": 1, "product_id": 7, "world": 0, "price": 10.0,
     "invoiced": datetime(2026, 3, 1)},
    {"client_id": 1, "product_id": 7, "world": 0, "price": 14.0,
     "invoiced": datetime(2026, 4, 1)},
    {"client_id": 1, "product_id": 7, "world": 0, "price": 18.0,
     "invoiced": datetime(2026, 5, 1)},
    {"client_id": 1, "product_id": 7, "world": 1, "price": 99.0,
     "invoiced": datetime(2026, 5, 2)},
    {"client_id": 1, "product_id": 7, "world": 0, "price": 55.0,
     "invoiced": datetime(2024, 1, 1)},
    {"client_id": 1, "product_id": 7, "world": 0, "price": 0.0,
     "invoiced": datetime(2026, 5, 3)},
    {"client_id": 2, "product_id": 7, "world": 0, "price": 77.0,
     "invoiced": datetime(2026, 5, 4)},
    {"client_id": 1, "product_id": 8, "world": 0, "price": 66.0,
     "invoiced": datetime(2026, 5, 5)},
]


def test_fallback_baseline_repository_on_synthetic_rows(monkeypatch):
    from app.data import pricing_repository

    monkeypatch.setattr(pricing_repository, "synthetic_product_id", lambda: 999)
    monkeypatch.setattr(
        pricing_repository,
        "query",
        _synthetic_fallback_query(_FALLBACK_LINES, _FALLBACK_AGREEMENTS),
    )
    same_world = pricing_repository.client_world_fallback_baseline(7, 11, "2026-06-15", 12)
    assert same_world == {"fallback_price": Decimal("14.0"), "n": 3}
    assert isinstance(same_world["fallback_price"], Decimal)
    assert pricing_repository.client_world_fallback_baseline(7, 12, "2026-06-15", 12) is None
    assert pricing_repository.client_world_fallback_baseline(7, 13, "2026-06-15", 12) is None
    assert pricing_repository.client_world_fallback_baseline(7, 404, "2026-06-15", 12) is None

    monkeypatch.setattr(
        pricing_repository, "query", lambda *a, **k: [{"fallback_price": None, "n": 0}]
    )
    assert pricing_repository.client_world_fallback_baseline(7, 11, "2026-06-15", 12) is None


def test_recommend_runs_fallback_path_end_to_end_on_synthetic_rows(monkeypatch):
    from app.data import pricing_repository

    real_baseline_price = pricing_repository.baseline_price
    _wire_repo(monkeypatch)
    monkeypatch.setattr(service.repo, "baseline_price", real_baseline_price)
    monkeypatch.setattr(pricing_repository, "synthetic_product_id", lambda: 999)
    monkeypatch.setattr(
        pricing_repository,
        "query",
        _synthetic_fallback_query(_FALLBACK_LINES, _FALLBACK_AGREEMENTS),
    )
    monkeypatch.setattr(service, "estimate_elasticity", lambda *a, **k: None)
    out = service.recommend_price(
        product_id=7, product_net_uid=None, client_agreement_net_uid="ca-uid",
        as_of_date="2026-06-15", use_cache=False,
    )
    assert out.baseline_source == "client_world_fallback"
    assert out.baseline_price == 14.0
    assert out.rationale != "no-baseline"

    monkeypatch.setattr(service.repo, "resolve_client_agreement", lambda uid: {
        "client_agreement_id": 12, "client_agreement_netuid": uid,
        "agreement_id": 22, "pricing_id": 849, "currency_id": 2})
    out = service.recommend_price(
        product_id=7, product_net_uid=None, client_agreement_net_uid="ca-uid",
        as_of_date="2026-06-15", use_cache=False,
    )
    assert out.baseline_source is None
    assert out.rationale == "no-baseline"


def test_floor_terms_follow_the_request_override(monkeypatch):
    _wire_repo(monkeypatch)
    out = service.recommend_price(
        product_id=7, product_net_uid=None, client_agreement_net_uid="ca-uid",
        as_of_date="2026-06-15", use_cache=False, target_margin_pct=25.0,
    )
    assert out.price_floor == 12.5
    assert out.floor_markup_pct == 25.0
    assert out.floor_margin_pct == 20.0


def test_cache_entry_without_floor_terms_is_recomputed(monkeypatch):
    from app.domain.models import PeerBand, PriceRecommendation

    _wire_repo(monkeypatch)
    settings = service.get_settings()
    coverage = service._history_fields(
        service.trailing_month_history_window(
            "2026-06-15",
            settings.trailing_window_months,
            settings.source_history_start_date,
        ),
        settings,
    )
    stale = PriceRecommendation(
        product_id=7, client_agreement_netuid="ca-uid", baseline_price=20.0,
        product_net_uid="p-uid", recommended_price=99.0, price_floor=11.2,
        unit_cost_eur=10.0, peer_band=PeerBand(), as_of_date="2026-06-15", **coverage,
    ).model_dump(mode="json")
    stale.pop("floor_markup_pct")
    stale.pop("floor_margin_pct")
    stale.pop("floor_basis")
    monkeypatch.setattr(service.cache, "get", lambda *a, **k: stale)
    monkeypatch.setattr(service.cache, "set", lambda *a, **k: None)
    out = service.recommend_price(
        product_id=7, product_net_uid=None, client_agreement_net_uid="ca-uid",
        as_of_date="2026-06-15", use_cache=True,
    )
    assert out.recommended_price == 18.5
    assert out.floor_margin_pct == 10.71
