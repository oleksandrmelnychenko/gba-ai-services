from __future__ import annotations

from app.services import portfolio, stock_health


def _stock_row() -> dict:
    return {
        "product_id": 42,
        "qty_on_hand": 100,
        "unit_cost_eur": 1,
        "eur_value": 100,
    }


def test_stock_health_uses_effective_factual_days_for_velocity(monkeypatch):
    monkeypatch.setattr(stock_health.sig, "on_hand_stock", lambda: [_stock_row()])
    monkeypatch.setattr(
        stock_health.sig,
        "sales_velocity",
        lambda *_args: [{"product_id": 42, "sold_qty": 10}],
    )
    monkeypatch.setattr(stock_health.sig, "sold_product_ids", lambda *_args: {42})

    snapshot = stock_health.snapshot("2025-01-11")
    row = snapshot["rows"][0]

    assert snapshot["history_windows"]["velocity"]["effective_days"] == 10
    assert snapshot["history_complete"] is False
    assert row["cover_days"] == 100.0
    assert row["band"] == "healthy"


def test_portfolio_uses_effective_factual_days_for_velocity(monkeypatch):
    monkeypatch.setattr(portfolio.sig, "on_hand_stock", lambda: [_stock_row()])
    monkeypatch.setattr(
        portfolio.sig,
        "sales_velocity",
        lambda *_args: [{"product_id": 42, "sold_qty": 10}],
    )
    monkeypatch.setattr(portfolio.sig, "sold_product_ids", lambda *_args: {42})
    monkeypatch.setattr(
        portfolio.sig,
        "avg_sale_price_eur",
        lambda *_args: [
            {
                "product_id": 42,
                "avg_price_eur": 2,
                "revenue_eur": 20,
                "sold_qty": 10,
            }
        ],
    )
    monkeypatch.setattr(portfolio.sig, "returns_for_products", lambda *_args: [])
    monkeypatch.setattr(
        portfolio.sig,
        "monthly_units",
        lambda *_args: [{"product_id": 42, "ym": "2025-01", "units": 10}],
    )

    build = portfolio.build_portfolio("2025-01-11")
    row = build["rows"][0]

    assert build["history_windows"]["velocity"]["effective_days"] == 10
    assert build["history_complete"] is False
    assert row["cover_days"] == 100.0
    assert row["band"] == "healthy"
