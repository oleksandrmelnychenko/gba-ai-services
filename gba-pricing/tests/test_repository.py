"""Repository tests — dbo.* is mocked (no live DB). Validates the cost fallback ladder and the
resolution helpers' return shapes; the SQL strings themselves are exercised live in smoke_test.
"""
from __future__ import annotations

from decimal import Decimal

from app.data import pricing_repository as repo


def test_unit_cost_prefers_median_onhand(monkeypatch):
    monkeypatch.setattr(repo, "query", lambda *a, **k: [
        {"median_cost": 10.5, "lot_count": 4, "latest_cost": 99.0}])
    out = repo.unit_cost_eur(123)
    assert out["unit_cost_eur"] == Decimal("10.5")
    assert out["cost_source"] == "median_onhand"
    assert out["lot_count"] == 4


def test_unit_cost_falls_back_to_latest_lot(monkeypatch):
    monkeypatch.setattr(repo, "query", lambda *a, **k: [
        {"median_cost": None, "lot_count": 0, "latest_cost": 22.0}])
    out = repo.unit_cost_eur(123)
    assert out["unit_cost_eur"] == Decimal("22.0")
    assert out["cost_source"] == "latest_lot"


def test_unit_cost_none_when_no_lot(monkeypatch):
    monkeypatch.setattr(repo, "query", lambda *a, **k: [
        {"median_cost": None, "lot_count": 0, "latest_cost": None}])
    out = repo.unit_cost_eur(123)
    assert out["unit_cost_eur"] is None
    assert out["cost_source"] == "none"


def test_resolve_product_by_id(monkeypatch):
    monkeypatch.setattr(repo, "synthetic_product_id", lambda: 999)
    monkeypatch.setattr(repo, "query", lambda *a, **k: [
        {"ID": 5, "NetUID": "abc"}])
    out = repo.resolve_product(5, None)
    assert out == {"id": 5, "net_uid": "abc"}


def test_resolve_product_missing(monkeypatch):
    monkeypatch.setattr(repo, "synthetic_product_id", lambda: 999)
    monkeypatch.setattr(repo, "query", lambda *a, **k: [])
    assert repo.resolve_product(None, "missing") is None
    assert repo.resolve_product(None, None) is None


def test_resolve_client_agreement_shape(monkeypatch):
    monkeypatch.setattr(repo, "query", lambda *a, **k: [{
        "client_agreement_id": 11, "client_agreement_netuid": "ca-uid",
        "agreement_id": 22, "pricing_id": 849, "currency_id": 2}])
    out = repo.resolve_client_agreement("ca-uid")
    assert out["client_agreement_id"] == 11
    assert out["agreement_id"] == 22
    assert out["pricing_id"] == 849
    assert out["currency_id"] == 2


def test_peer_band_empty(monkeypatch):
    monkeypatch.setattr(repo, "query", lambda *a, **k: [])
    out = repo.peer_band(1, "2026-06-15", 12, "2026-06-15")
    assert out == {"p25": None, "p50": None, "p75": None, "n": 0}


def test_peer_band_shape(monkeypatch):
    monkeypatch.setattr(repo, "query", lambda *a, **k: [
        {"p25": 8.35, "p50": 8.64, "p75": 8.91, "n": 66}])
    out = repo.peer_band(25348486, "2026-06-15", 12, "2026-06-15")
    assert out == {
        "p25": Decimal("8.35"),
        "p50": Decimal("8.64"),
        "p75": Decimal("8.91"),
        "n": 66,
    }


def test_peer_band_passes_mad_params(monkeypatch):
    captured = {}

    def fake_query(sql, params=None):
        captured["sql"] = sql
        captured["params"] = params
        return [{"p25": 1.0, "p50": 2.0, "p75": 3.0, "n": 5}]

    monkeypatch.setattr(repo, "query", fake_query)
    repo.peer_band(1, "2026-06-15", 12, "2026-06-15")
    assert "mad_k" in captured["params"]
    assert "min_rows" in captured["params"]
    assert "1.4826" in captured["sql"]


def test_peer_band_uses_priceperitem_directly_no_fx_conversion():
    import inspect

    src = inspect.getsource(repo.peer_band)
    assert "oi.PricePerItem AS eur_price" in src
    assert "dbo.GetExchangedToEuroValue(oi.PricePerItem" not in src
    assert "WHEN a.CurrencyID = 2 THEN oi.PricePerItem" not in src


def test_base_list_markup_reads_agreement_tier_extra_charge(monkeypatch):
    monkeypatch.setattr(repo, "query", lambda *a, **k: [{
        "base_price": 32.22, "extra_charge": 30.0,
        "base_pricing_id": 845, "culture": "uk"}])
    out = repo.base_list_price_and_markup(26161448, 22)
    assert out["base_price"] == Decimal("32.22")
    assert out["extra_charge"] == 30.0
    assert out["base_pricing_id"] == 845
    assert out["culture"] == "uk"


def test_base_list_markup_extra_charge_from_agreement_pricing_in_sql():
    import inspect

    src = inspect.getsource(repo.base_list_price_and_markup)
    assert "agr_pr.ID = a.PricingID" in src
    assert "agr_pr.CalculatedExtraCharge" in src
    assert "PricingProductGroupDiscount" in src


def test_base_list_markup_uses_the_agreement_worlds_source_definition():
    import inspect

    src = inspect.getsource(repo.base_list_price_and_markup)
    assert "psd.PricingID = agr_pr.ID AND psd.ImportedForAmg = world.for_amg" in src
    assert "{AGREEMENT_WORLD}" in src
    assert "ELSE org.PriceSourceIsAmg" in repo.AGREEMENT_WORLD
    assert src.index("ppgd.CalculatedExtraCharge IS NOT NULL") < src.index("world_def.CalculatedExtraCharge IS NOT NULL")
    assert src.index("world_def.CalculatedExtraCharge IS NOT NULL") < src.index("ELSE agr_pr.CalculatedExtraCharge")


def test_segment_discount_distribution_shape(monkeypatch):
    monkeypatch.setattr(repo, "query", lambda *a, **k: [
        {"p75": 12.0, "p90": 18.0, "n": 40}])
    out = repo.segment_discount_distribution(106, 849, "uk")
    assert out["p75"] == 12.0
    assert out["p90"] == 18.0
    assert out["n"] == 40


def test_active_group_discount(monkeypatch):
    monkeypatch.setattr(repo, "query", lambda *a, **k: [{"DiscountRate": 8.5}])
    assert repo.active_group_discount(11, 106) == 8.5
    monkeypatch.setattr(repo, "query", lambda *a, **k: [])
    assert repo.active_group_discount(11, 106) is None


def test_resolve_product_requires_live_matching_non_synthetic_identity(monkeypatch):
    captured = {}
    monkeypatch.setattr(repo, "synthetic_product_id", lambda: 777)

    def fake_query(sql, params=None):
        captured["sql"] = sql
        captured["params"] = params
        return [{"ID": 5, "NetUID": "abc"}]

    monkeypatch.setattr(repo, "query", fake_query)
    assert repo.resolve_product(5, "abc") == {"id": 5, "net_uid": "abc"}
    assert "p.Deleted = 0" in captured["sql"]
    assert "p.ID <> :synthetic" in captured["sql"]
    assert "p.ID = :pid" in captured["sql"]
    assert "p.NetUID = :uid" in captured["sql"]
    assert captured["params"] == {"pid": 5, "uid": "abc", "synthetic": 777}


def test_resolve_product_defensively_rejects_dynamic_synthetic_row(monkeypatch):
    monkeypatch.setattr(repo, "synthetic_product_id", lambda: 777)
    monkeypatch.setattr(
        repo,
        "query",
        lambda *a, **k: [{"ID": 777, "NetUID": "debt-uid"}],
    )
    assert repo.resolve_product(777, None) is None


def test_repository_money_aggregates_remain_decimal(monkeypatch):
    monkeypatch.setattr(repo, "synthetic_product_id", lambda: 999)
    monkeypatch.setattr(
        repo,
        "query",
        lambda *a, **k: [{"baseline_price": 1.005}],
    )
    assert repo.baseline_price("p", "ca", "uk", True) == Decimal("1.005")


def test_segment_discount_distribution_cached_per_segment_and_generation(monkeypatch):
    calls = []

    def fake_query(sql, params=None):
        calls.append(dict(params))
        return [{"p75": 12.0, "p90": 18.0, "n": 40}]

    generation = {"value": "daily-2026-11-12"}
    monkeypatch.setattr(repo, "query", fake_query)
    monkeypatch.setattr(repo, "refresh_generation", lambda: generation["value"])

    first = repo.segment_discount_distribution(106, 849, "uk")
    first["p75"] = -1.0
    second = repo.segment_discount_distribution(106, 849, "uk")
    assert second == {"p75": 12.0, "p90": 18.0, "n": 40}
    assert len(calls) == 1

    repo.segment_discount_distribution(107, 849, "uk")
    repo.segment_discount_distribution(106, 850, "uk")
    repo.segment_discount_distribution(106, 849, "pl")
    assert len(calls) == 4

    generation["value"] = "daily-2026-11-13"
    repo.segment_discount_distribution(106, 849, "uk")
    assert len(calls) == 5
    assert all(key[3] == "daily-2026-11-13" for key in repo._segment_cache)


def test_segment_discount_distribution_caches_empty_segment(monkeypatch):
    calls = []

    def fake_query(sql, params=None):
        calls.append(1)
        return []

    monkeypatch.setattr(repo, "query", fake_query)
    assert repo.segment_discount_distribution(1, 2, "uk") == {"p75": None, "p90": None, "n": 0}
    assert repo.segment_discount_distribution(1, 2, "uk") == {"p75": None, "p90": None, "n": 0}
    assert len(calls) == 1


def test_segment_discount_distribution_resolves_tier_family_once_not_per_row():
    import inspect

    src = inspect.getsource(repo.segment_discount_distribution)
    assert "dbo.GetBasePricingId(p.ID) = :base_pricing_id" in src
    assert "GetBasePricingId(a.PricingID)" not in src
    assert "JOIN fam ON fam.ID = a.PricingID" in src
    assert "pr.ID = :base_pricing_id AND pr.Culture = :culture" in src


def test_synthetic_product_lookup_failure_never_falls_back_to_a_configured_id(monkeypatch):
    from app.core.config import Settings

    def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(repo, "query", boom)
    monkeypatch.setattr(repo, "_synthetic_id", None)
    monkeypatch.setattr(repo, "_synthetic_expires_at", 0.0)
    assert repo.synthetic_product_id() == 0
    assert "synthetic_line_product_id" not in Settings.model_fields


def test_synthetic_product_keeps_last_resolved_id_when_lookup_fails(monkeypatch):
    monkeypatch.setattr(repo, "_synthetic_id", None)
    monkeypatch.setattr(repo, "_synthetic_expires_at", 0.0)
    monkeypatch.setattr(repo, "query", lambda *a, **k: [{"ID": 38774381}])
    assert repo.synthetic_product_id() == 38774381
    monkeypatch.setattr(repo, "_synthetic_expires_at", 0.0)

    def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(repo, "query", boom)
    assert repo.synthetic_product_id() == 38774381


def test_peer_band_reads_sale_lines_once_into_a_session_scoped_table(monkeypatch):
    captured = {}

    def fake_query(sql, params=None):
        captured["sql"] = sql
        return []

    monkeypatch.setattr(repo, "query", fake_query)
    monkeypatch.setattr(repo, "synthetic_product_id", lambda: 1)
    assert repo.peer_band(7, "2026-06-15", 12, "2026-06-15")["n"] == 0
    sql = captured["sql"]
    assert sql.count("FROM dbo.OrderItem oi") == 1
    assert sql.index("DROP TABLE IF EXISTS #priced") < sql.index("INTO #priced")
    assert "WITH priced AS (SELECT ca_id, eur_price FROM #priced)" in sql
