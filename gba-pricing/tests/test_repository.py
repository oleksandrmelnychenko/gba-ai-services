"""Repository tests — dbo.* is mocked (no live DB). Validates the cost fallback ladder and the
resolution helpers' return shapes; the SQL strings themselves are exercised live in smoke_test.
"""
from __future__ import annotations

from app.data import pricing_repository as repo


def test_unit_cost_prefers_median_onhand(monkeypatch):
    monkeypatch.setattr(repo, "query", lambda *a, **k: [
        {"median_cost": 10.5, "lot_count": 4, "latest_cost": 99.0}])
    out = repo.unit_cost_eur(123)
    assert out["unit_cost_eur"] == 10.5
    assert out["cost_source"] == "median_onhand"
    assert out["lot_count"] == 4


def test_unit_cost_falls_back_to_latest_lot(monkeypatch):
    monkeypatch.setattr(repo, "query", lambda *a, **k: [
        {"median_cost": None, "lot_count": 0, "latest_cost": 22.0}])
    out = repo.unit_cost_eur(123)
    assert out["unit_cost_eur"] == 22.0
    assert out["cost_source"] == "latest_lot"


def test_unit_cost_none_when_no_lot(monkeypatch):
    monkeypatch.setattr(repo, "query", lambda *a, **k: [
        {"median_cost": None, "lot_count": 0, "latest_cost": None}])
    out = repo.unit_cost_eur(123)
    assert out["unit_cost_eur"] is None
    assert out["cost_source"] == "none"


def test_resolve_product_by_id(monkeypatch):
    monkeypatch.setattr(repo, "query", lambda *a, **k: [
        {"ID": 5, "NetUID": "abc"}])
    out = repo.resolve_product(5, None)
    assert out == {"id": 5, "net_uid": "abc"}


def test_resolve_product_missing(monkeypatch):
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
    assert out == {"p25": 8.35, "p50": 8.64, "p75": 8.91, "n": 66}


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
    assert out["base_price"] == 32.22
    assert out["extra_charge"] == 30.0
    assert out["base_pricing_id"] == 845
    assert out["culture"] == "uk"


def test_base_list_markup_extra_charge_from_agreement_pricing_in_sql():
    import inspect

    src = inspect.getsource(repo.base_list_price_and_markup)
    assert "agr_pr.ID = a.PricingID" in src
    assert "agr_pr.CalculatedExtraCharge" in src
    assert "PricingProductGroupDiscount" in src


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
