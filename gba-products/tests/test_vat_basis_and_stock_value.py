from __future__ import annotations

import inspect

import pytest

from app.data import canonical_sales, signals_repository
from app.services import history_policy, margin_returns, portfolio, stock_health


def _build(monkeypatch, stock, prices, as_of="2026-07-25"):
    sold = [row["product_id"] for row in prices]
    monkeypatch.setattr(portfolio.sig, "on_hand_stock", lambda: stock)
    monkeypatch.setattr(portfolio.sig, "sales_velocity",
                        lambda *_: [{"product_id": pid, "sold_qty": 10} for pid in sold])
    monkeypatch.setattr(portfolio.sig, "sold_product_ids", lambda *_: set(sold))
    monkeypatch.setattr(portfolio.sig, "avg_sale_price_eur", lambda *_: prices)
    monkeypatch.setattr(portfolio.sig, "returns_for_products", lambda *_: [])
    monkeypatch.setattr(portfolio.sig, "monthly_units",
                        lambda *_: [{"product_id": pid, "ym": "2026-07", "units": 10} for pid in sold])
    return portfolio.build_portfolio(as_of)


def _stock(pid, cost, cost_ex_vat, qty=5):
    return {"product_id": pid, "qty_on_hand": qty, "unit_cost_eur": cost,
            "unit_cost_ex_vat_eur": cost_ex_vat, "eur_value": None if cost is None else qty * cost}


def _price(pid, gross, ex_vat, units=10, unknown=0, vat_units=None):
    if vat_units is None:
        vat_units = units if ex_vat is not None and ex_vat < gross else 0
    return {"product_id": pid, "avg_price_eur": gross, "revenue_eur": gross * units, "sold_qty": units,
            "revenue_ex_vat_eur": None if ex_vat is None else ex_vat * units,
            "ex_vat_unknown_lines": unknown, "vat_sold_qty": vat_units}


@pytest.mark.parametrize(
    ("case", "cost", "cost_ex_vat", "gross", "ex_vat", "matched_cost", "margin", "profit"),
    [
        ("vat sale, lot with recorded vat", 9.6, 8, 12, 10, 8, 0.2, 20),
        ("vat sale, lot without recorded vat: 16.7% before, 0% now", 10, 10, 12, 10, 10, 0.0, 0),
        ("sale without vat, lot with recorded vat stays a cost", 9.6, 8, 10, 10, 9.6, 0.04, 4),
        ("sale without vat, lot without recorded vat", 9.6, 9.6, 10, 10, 9.6, 0.04, 4),
    ],
)
def test_margin_compares_revenue_and_cost_without_recoverable_vat(
        monkeypatch, case, cost, cost_ex_vat, gross, ex_vat, matched_cost, margin, profit):
    row = _build(monkeypatch, [_stock(1, cost, cost_ex_vat)], [_price(1, gross, ex_vat)])["rows"][0]
    assert row["margin_pct"] == pytest.approx(margin, abs=1e-6), case
    assert row["margin_ex_vat_eur"] == profit
    assert row["margin_vat_basis"] == "ex-vat"
    assert (row["unit_cost_eur"], row["avg_price_eur"]) == (cost, gross)
    assert (row["unit_cost_ex_vat_eur"], row["avg_price_ex_vat_eur"]) == (cost_ex_vat, ex_vat)
    assert row["margin_unit_cost_eur"] == matched_cost
    summary = margin_returns.margin_returns_summary([row])
    assert summary["estimated_profit_eur"] == profit
    assert summary["revenue_ex_vat_eur_known_margin"] == ex_vat * 10
    assert summary["weighted_avg_margin_pct"] == pytest.approx(margin, abs=1e-6)
    enriched = margin_returns.margin_leaders([row])[0]
    assert enriched["margin_eur"] == pytest.approx(row["margin_pct"] * row["revenue_eur"], abs=0.006)
    assert enriched["margin_ex_vat_eur"] == profit


def test_mixed_sales_match_each_part_to_its_own_cost_basis(monkeypatch):
    price = _price(1, 11.2, 10, units=10, vat_units=6)
    row = _build(monkeypatch, [_stock(1, 9.6, 8)], [price])["rows"][0]
    assert row["margin_unit_cost_eur"] == pytest.approx((6 * 8 + 4 * 9.6) / 10, abs=1e-4)
    assert row["margin_ex_vat_eur"] == pytest.approx(100 - 6 * 8 - 4 * 9.6, abs=0.005)
    assert row["margin_pct"] == pytest.approx((100 - 86.4) / 100, abs=1e-6)


def test_margin_is_unknown_when_a_vat_line_has_no_price_without_vat(monkeypatch):
    row = _build(monkeypatch, [_stock(1, 9.6, 8)], [_price(1, 12, 9, unknown=1)])["rows"][0]
    assert row["margin_pct"] is None and row["margin_ex_vat_eur"] is None
    assert row["revenue_ex_vat_eur"] is None and row["revenue_eur"] == 120


def test_stock_without_cost_is_counted_not_valued_at_zero(monkeypatch):
    build = _build(
        monkeypatch,
        [_stock(1, 2, 2, qty=5), _stock(2, None, None, qty=7)],
        [_price(1, 12, 10), _price(3, 12, 10)],
    )
    rows = {row["product_id"]: row for row in build["rows"]}
    assert rows[1]["stock_value_eur"] == 10 and rows[2]["stock_value_eur"] is None
    assert rows[2]["eur_value"] == 0 and rows[3]["stock_value_eur"] == 0
    overview = build["overview"]
    assert overview["total_skus"] == 3 and overview["stock_skus"] == 2
    assert (overview["stock_valued_skus"], overview["stock_unvalued_skus"]) == (1, 1)
    assert overview["stock_unvalued_qty"] == 7
    assert overview["stock_value_eur"] == 10 and overview["stock_value_complete"] is False
    assert overview["total_eur_value"] == 10


def test_stock_value_is_null_when_no_stocked_sku_has_a_cost(monkeypatch):
    overview = _build(monkeypatch, [_stock(2, None, None, qty=7)], [])["overview"]
    assert overview["stock_value_eur"] is None and overview["total_eur_value"] == 0
    assert overview["stock_unvalued_skus"] == 1


def test_a_past_as_of_states_that_stock_is_current(monkeypatch):
    today = history_policy.stock_observation("2999-01-01")["stock_as_of"]
    past = _build(monkeypatch, [_stock(1, 2, 2)], [], as_of="2026-01-10")["overview"]
    assert past["stock_as_of"] == today and past["stock_basis"] == "current"
    assert past["stock_matches_as_of"] is False
    assert history_policy.stock_observation(today)["stock_matches_as_of"] is True
    monkeypatch.setattr(stock_health.sig, "on_hand_stock", lambda: [_stock(1, 2, 2)])
    monkeypatch.setattr(stock_health.sig, "sales_velocity", lambda *_: [])
    monkeypatch.setattr(stock_health.sig, "sold_product_ids", lambda *_: set())
    snapshot = stock_health.snapshot("2026-01-10")
    assert snapshot["stock_as_of"] == today and snapshot["stock_matches_as_of"] is False


def test_sql_takes_vat_out_of_both_sides_by_the_recorded_facts():
    stock_sql = inspect.getsource(signals_repository.on_hand_stock)
    assert "pack.ID = income_item.PackingListPackageOrderItemID" in stock_sql
    assert "CASE WHEN pack.VatPercent > 0" in signals_repository._COST_PRICE_EX_VAT
    assert "1 + pack.VatPercent / 100.0" in signals_repository._COST_PRICE_EX_VAT
    facts = " ".join(canonical_sales.facts_cte().split())
    assert "SIGN(oi.PricePerItemWithoutVat) = SIGN(oi.PricePerItem) THEN oi.PricePerItemWithoutVat" in facts
    assert "WHEN oi.PricePerItem = 0 OR s.IsVatSale = 0 THEN oi.PricePerItem END" in facts
    assert "AS revenue_ex_vat_eur" in facts and "AS ex_vat_unknown_lines" in facts
    assert "< ABS(oi.PricePerItem) THEN CAST(oi.Qty AS decimal(30,14)) ELSE 0 END AS vat_sold_qty" in facts
