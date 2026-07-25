from __future__ import annotations

from decimal import Decimal

import pytest

from app.api import main
from app.services import portfolio, stock_health


def _portfolio_result() -> dict:
    row = {
        "product_id": 10,
        "eur_value": 1.01,
        "revenue_eur": 2.01,
    }
    return {
        "count": 1,
        "rows": [row],
        "overview": {
            "total_eur_value": 1.01,
            "total_revenue_eur": 2.01,
            "by_band": {"healthy": 1},
            "by_lifecycle": {"mature": 1},
            "by_action": {"keep": 1},
            "by_abc": {"A": 1},
            "by_xyz": {"X": 1},
        },
    }


def test_product_index_rejects_duplicate_or_invalid_identity():
    with pytest.raises(ValueError, match="duplicate product_id 10"):
        portfolio._index_by_product(
            [{"product_id": 10}, {"product_id": 10}],
            "test_source",
        )
    with pytest.raises(ValueError, match="positive integer"):
        portfolio._index_by_product([{"product_id": 0}], "test_source")


def test_portfolio_validator_rejects_count_and_money_total_drift():
    valid = _portfolio_result()
    portfolio._validate_portfolio_result(valid)

    wrong_count = _portfolio_result()
    wrong_count["count"] = 2
    with pytest.raises(ValueError, match="count"):
        portfolio._validate_portfolio_result(wrong_count)

    wrong_total = _portfolio_result()
    wrong_total["overview"]["total_revenue_eur"] = 2.0
    with pytest.raises(ValueError, match="total_revenue_eur"):
        portfolio._validate_portfolio_result(wrong_total)


def test_stock_validator_rejects_band_count_or_aggregate_drift():
    snapshot = {
        "total_skus": 1,
        "total_qty": 2.0,
        "total_eur_value": 1.01,
        "valued_skus": 1,
        "unvalued_skus": 0,
        "rows": [
            {
                "product_id": 10,
                "qty_on_hand": 2.0,
                "eur_value": 1.01,
            }
        ],
        "bands": {
            "healthy": {
                "count": 1,
                "qty": 2.0,
                "eur_value": 1.01,
            }
        },
    }
    stock_health._validate_snapshot(snapshot)

    snapshot["bands"]["healthy"]["eur_value"] = 1.0
    with pytest.raises(ValueError, match="EUR total"):
        stock_health._validate_snapshot(snapshot)


def test_regional_normalizer_rounds_half_up_and_rejects_duplicate_identity():
    row = {
        "product_id": 10,
        "region_id": 20,
        "regional_units": Decimal("2.34565"),
        "regional_revenue_eur": Decimal("1.005"),
        "regional_order_count": 3,
        "regional_client_count": 2,
    }

    normalized = main._normalize_product_region_rows([row])

    assert normalized[0]["regional_units"] == 2.3457
    assert normalized[0]["regional_revenue_eur"] == 1.01
    with pytest.raises(ValueError, match="duplicate"):
        main._normalize_product_region_rows([row, row])
