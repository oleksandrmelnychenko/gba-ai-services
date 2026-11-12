from __future__ import annotations

import pytest

from app.services import portfolio


@pytest.mark.parametrize(
    ("returned_qty", "expected_rate"),
    [
        (1.0, 0.2),  # a partial return must stay partial, not become the sold Qty=5
        (3.0, 0.6),  # two active return rows (1 + 2) must remain their canonical sum
    ],
)
def test_portfolio_preserves_canonical_return_quantity(
    monkeypatch, returned_qty, expected_rate
):
    product_id = 42
    as_of = "2026-07-25"
    monkeypatch.setattr(portfolio.sig, "on_hand_stock", lambda: [])
    monkeypatch.setattr(
        portfolio.sig,
        "sales_velocity",
        lambda *_args: [{"product_id": product_id, "sold_qty": 5.0}],
    )
    monkeypatch.setattr(portfolio.sig, "sold_product_ids", lambda *_args: {product_id})
    monkeypatch.setattr(
        portfolio.sig,
        "avg_sale_price_eur",
        lambda *_args: [
            {
                "product_id": product_id,
                "avg_price_eur": 10.0,
                "revenue_eur": 50.0,
                "sold_qty": 5.0,
            }
        ],
    )
    monkeypatch.setattr(
        portfolio.sig,
        "returns_for_products",
        lambda *_args: [{"product_id": product_id, "returned_qty": returned_qty, "known_returned_value_eur": 7.5, "returned_value_eur": 7.5, "unresolved_amount_count": 0}],
    )
    monkeypatch.setattr(
        portfolio.sig,
        "monthly_units",
        lambda *_args: [{"product_id": product_id, "ym": "2026-07", "units": 5.0}],
    )

    row = portfolio.build_portfolio(as_of)["rows"][0]

    assert row["annual_units"] == 5.0
    assert row["return_rate"] == expected_rate
    assert row["net_revenue_eur"] == 42.5
    assert row["gross_revenue_eur"] == 50.0



def test_signed_gross_adjustment_does_not_hide_portfolio_finances(monkeypatch):
    monkeypatch.setattr(portfolio.sig, "on_hand_stock", lambda: [])
    monkeypatch.setattr(portfolio.sig, "sales_velocity", lambda *_: [{"product_id": 42, "sold_qty": -2}])
    monkeypatch.setattr(portfolio.sig, "sold_product_ids", lambda *_: {42})
    monkeypatch.setattr(portfolio.sig, "avg_sale_price_eur", lambda *_: [{"product_id": 42, "sold_qty": -2, "revenue_eur": -20, "avg_price_eur": 10}])
    monkeypatch.setattr(portfolio.sig, "returns_for_products", lambda *_: [])
    monkeypatch.setattr(portfolio.sig, "monthly_units", lambda *_: [{"product_id": 42, "ym": "2026-07", "units": -2}])
    result = portfolio.build_portfolio("2026-07-25")
    row = result["rows"][0]
    assert row["revenue_eur"] == row["net_revenue_eur"] == -20
    assert row["annual_units"] == -2
    assert row["abc"] == "unknown"
    assert row["demand_adjustments_present"] is True
    assert result["overview"]["total_net_revenue_eur"] == -20
    from app.services.margin_returns import margin_returns_summary
    assert margin_returns_summary([row])["total_annual_units"] == -2


def test_public_units_and_return_rate_share_their_own_sales_window(monkeypatch):
    monkeypatch.setattr(portfolio.sig, "on_hand_stock", lambda: [])
    monkeypatch.setattr(portfolio.sig, "sales_velocity", lambda *_: [])
    monkeypatch.setattr(portfolio.sig, "sold_product_ids", lambda *_: {42})
    monkeypatch.setattr(portfolio.sig, "avg_sale_price_eur", lambda *_: [{"product_id": 42, "sold_qty": 10, "revenue_eur": 100, "avg_price_eur": 10}])
    monkeypatch.setattr(portfolio.sig, "returns_for_products", lambda *_: [{"product_id": 42, "returned_qty": 2, "known_returned_value_eur": 5, "unresolved_amount_count": 0}])
    monkeypatch.setattr(portfolio.sig, "monthly_units", lambda *_: [{"product_id": 42, "ym": "2026-07", "units": 4}])
    row = portfolio.build_portfolio("2026-07-25")["rows"][0]
    assert row["annual_units"] == 10
    assert row["classification_units"] == 4
    assert row["return_rate"] == 0.2
    assert row["net_revenue_eur"] == 95


def _lifecycle_portfolio(monkeypatch, monthly_rows):
    captured = {}

    def monthly_units(_as_of, months):
        captured["months"] = months
        return monthly_rows

    monkeypatch.setattr(portfolio.sig, "on_hand_stock", lambda: [])
    monkeypatch.setattr(portfolio.sig, "sales_velocity", lambda *_: [{"product_id": 42, "sold_qty": 3}])
    monkeypatch.setattr(portfolio.sig, "sold_product_ids", lambda *_: {42})
    monkeypatch.setattr(portfolio.sig, "avg_sale_price_eur", lambda *_: [{"product_id": 42, "sold_qty": 3, "revenue_eur": 30, "avg_price_eur": 10}])
    monkeypatch.setattr(portfolio.sig, "returns_for_products", lambda *_: [])
    monkeypatch.setattr(portfolio.sig, "monthly_units", monthly_units)
    result = portfolio.build_portfolio("2026-07-25")
    return result, captured


def test_first_sale_this_quarter_is_new(monkeypatch):
    result, captured = _lifecycle_portfolio(
        monkeypatch, [{"product_id": 42, "ym": "2026-07", "units": 3}])
    assert captured["months"] >= 60
    assert result["rows"][0]["lifecycle"] == "new"


def test_returning_product_sold_before_the_classification_window_is_not_new(monkeypatch):
    result, _ = _lifecycle_portfolio(monkeypatch, [
        {"product_id": 42, "ym": "2025-03", "units": 2},
        {"product_id": 42, "ym": "2026-07", "units": 3},
    ])
    row = result["rows"][0]
    assert row["lifecycle"] == "mature"
    assert row["classification_units"] == 3


def test_history_only_products_do_not_join_the_portfolio(monkeypatch):
    result, _ = _lifecycle_portfolio(monkeypatch, [
        {"product_id": 42, "ym": "2026-07", "units": 3},
        {"product_id": 77, "ym": "2025-02", "units": 9},
    ])
    assert [row["product_id"] for row in result["rows"]] == [42]


def test_months_before_the_source_floor_are_still_rejected(monkeypatch):
    import pytest
    with pytest.raises(ValueError, match="unexpected month"):
        _lifecycle_portfolio(monkeypatch, [{"product_id": 42, "ym": "2024-12", "units": 3}])
