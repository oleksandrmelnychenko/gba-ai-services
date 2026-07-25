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
        lambda *_args: [{"product_id": product_id, "returned_qty": returned_qty}],
    )
    monkeypatch.setattr(
        portfolio.sig,
        "monthly_units",
        lambda *_args: [{"product_id": product_id, "ym": "2026-07", "units": 5.0}],
    )

    row = portfolio.build_portfolio(as_of)["rows"][0]

    assert row["annual_units"] == 5.0
    assert row["return_rate"] == expected_rate
