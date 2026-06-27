"""Lens 3 substitution tests.

`substitutes()` is exercised purely with a stubbed catalog_repository + a synthetic health_lookup
(no DB, no portfolio import). A separate integration-marked test hits catalog_repository live.
"""
from __future__ import annotations

import pytest

from app.services import substitution


def _stub_catalog(monkeypatch, target, candidates):
    monkeypatch.setattr(substitution.cat, "product_card", lambda pid: target)
    monkeypatch.setattr(substitution.cat, "analogues_for", lambda pid: list(candidates))


def _cand(pid, *, name="x", vc="VC", oe="", for_sale=True, source="analogue"):
    return {"product_id": pid, "name": name, "vendor_code": vc, "oe_number": oe,
            "is_for_sale": for_sale, "source": source}


def test_missing_target_returns_not_found(monkeypatch):
    _stub_catalog(monkeypatch, None, [])
    out = substitution.substitutes(999, {})
    assert out["found"] is False
    assert out["count"] == 0 and out["candidates"] == [] and out["in_stock_count"] == 0


def test_in_stock_candidate_ranks_above_order_to_demand(monkeypatch):
    target = {"product_id": 1, "name": "t", "vendor_code": "T", "oe_number": "OE123",
              "has_analogue": True, "is_for_sale": True}
    _stub_catalog(monkeypatch, target, [_cand(10), _cand(11)])
    health = {
        10: {"band": "order_to_demand", "qty_on_hand": 0, "health": 90.0},  # not in stock
        11: {"band": "healthy", "qty_on_hand": 5, "health": 40.0},          # in stock, lower health
    }
    out = substitution.substitutes(1, health)
    assert out["found"] is True
    assert [c["product_id"] for c in out["candidates"]] == [11, 10]
    assert out["candidates"][0]["in_stock"] is True
    assert out["candidates"][1]["in_stock"] is False
    assert out["in_stock_count"] == 1


def test_order_to_demand_with_qty_counts_as_in_stock(monkeypatch):
    # band order_to_demand but a positive on-hand qty still means we can supply it now
    target = {"product_id": 1, "name": "t", "vendor_code": "T", "oe_number": "OE",
              "has_analogue": True, "is_for_sale": True}
    _stub_catalog(monkeypatch, target, [_cand(20)])
    out = substitution.substitutes(1, {20: {"band": "order_to_demand", "qty_on_hand": 3, "health": 50.0}})
    assert out["candidates"][0]["in_stock"] is True
    assert out["in_stock_count"] == 1


def test_health_breaks_ties_among_in_stock(monkeypatch):
    target = {"product_id": 1, "name": "t", "vendor_code": "T", "oe_number": "OE",
              "has_analogue": True, "is_for_sale": True}
    _stub_catalog(monkeypatch, target, [_cand(30), _cand(31), _cand(32)])
    health = {
        30: {"band": "healthy", "qty_on_hand": 1, "health": 55.0},
        31: {"band": "slow", "qty_on_hand": 1, "health": 80.0},
        32: {"band": "overstock", "qty_on_hand": 1, "health": 70.0},
    }
    out = substitution.substitutes(1, health)
    assert [c["product_id"] for c in out["candidates"]] == [31, 32, 30]


def test_unknown_and_not_for_sale_rank_below_known_in_stock(monkeypatch):
    target = {"product_id": 1, "name": "t", "vendor_code": "T", "oe_number": "OE",
              "has_analogue": True, "is_for_sale": True}
    cands = [
        _cand(40, for_sale=True),                    # in portfolio, in stock
        _cand(41, for_sale=False, source="oe"),      # not for sale, not in lookup
        _cand(42, for_sale=True),                    # for sale but unknown to lookup
    ]
    _stub_catalog(monkeypatch, target, cands)
    health = {40: {"band": "healthy", "qty_on_hand": 2, "health": 30.0}}
    out = substitution.substitutes(1, health)
    ordered = [c["product_id"] for c in out["candidates"]]
    assert ordered[0] == 40                       # only true in-stock candidate first
    assert ordered[1] == 42                       # for-sale unknown beats not-for-sale
    assert ordered[2] == 41
    assert out["candidates"][2]["in_portfolio"] is False
    assert out["candidates"][2]["in_stock"] is False


def test_analogue_source_beats_oe_on_full_tie(monkeypatch):
    target = {"product_id": 1, "name": "t", "vendor_code": "T", "oe_number": "OE",
              "has_analogue": True, "is_for_sale": True}
    # identical sellability/health -> curated analogue link wins the tie-break over OE-only
    _stub_catalog(monkeypatch, target, [_cand(50, source="oe"), _cand(51, source="analogue")])
    health = {50: {"band": "healthy", "qty_on_hand": 1, "health": 60.0},
              51: {"band": "healthy", "qty_on_hand": 1, "health": 60.0}}
    out = substitution.substitutes(1, health)
    assert [c["product_id"] for c in out["candidates"]] == [51, 50]


def test_limit_truncates_after_ranking(monkeypatch):
    target = {"product_id": 1, "name": "t", "vendor_code": "T", "oe_number": "OE",
              "has_analogue": True, "is_for_sale": True}
    _stub_catalog(monkeypatch, target, [_cand(60), _cand(61), _cand(62)])
    health = {
        60: {"band": "order_to_demand", "qty_on_hand": 0, "health": 10.0},
        61: {"band": "healthy", "qty_on_hand": 1, "health": 90.0},
        62: {"band": "healthy", "qty_on_hand": 1, "health": 50.0},
    }
    out = substitution.substitutes(1, health, limit=2)
    assert [c["product_id"] for c in out["candidates"]] == [61, 62]
    assert out["count"] == 2


# ----------------------------------------------------------------------------- integration (live DB)

pytestmark_integration = pytest.mark.integration


@pytest.mark.integration
def test_analogues_for_live_target():
    """Live: target 25804318 (Фонарь задний правый) has a curated analogue set incl. an OE-only add."""
    try:
        from app.data import catalog_repository as cat
        from app.data.db import query
        query("SELECT 1 AS ok")
    except Exception:
        pytest.skip("dev DB not reachable")

    card = cat.product_card(25804318)
    assert card and card["is_for_sale"] and card["has_analogue"]
    cands = cat.analogues_for(25804318)
    assert len(cands) > 100
    assert all(int(c["product_id"]) != 25804318 for c in cands)
    sources = {c["source"] for c in cands}
    assert "analogue" in sources  # curated links present

    # substitutes() with an empty lookup must still rank and never raise
    out = substitution.substitutes(25804318, {})
    assert out["found"] is True and out["count"] == len(cands)
    assert out["in_stock_count"] == 0  # empty lookup => nothing known to be in stock
