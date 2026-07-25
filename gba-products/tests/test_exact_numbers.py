from __future__ import annotations

from decimal import Decimal

import pytest

from app.core import exact_numbers as exact


def test_currency_uses_accounting_half_up_not_bankers_rounding():
    assert exact.money(Decimal("1.004")) == 1.0
    assert exact.money(Decimal("1.005")) == 1.01
    assert exact.money(Decimal("2.675")) == 2.68
    assert exact.money(Decimal("-1.005"), non_negative=False) == -1.01


def test_unit_price_and_quantity_have_explicit_scales():
    assert exact.unit_price(Decimal("1.23445")) == 1.2345
    assert exact.quantity(Decimal("2.34565")) == 2.3457
    assert exact.ratio(Decimal("0.12345")) == 0.1235


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), "bad"])
def test_numeric_boundary_rejects_non_finite_or_invalid_values(value):
    with pytest.raises(ValueError):
        exact.money(value, non_negative=False)


def test_displayed_currency_aggregate_is_sum_of_displayed_line_cents():
    lines = [
        exact.money(Decimal("1.005")),
        exact.money(Decimal("2.005")),
        exact.money(Decimal("3.004")),
    ]

    total = exact.money(
        exact.decimal_sum(lines, "line money", non_negative=True),
        "total money",
    )

    assert lines == [1.01, 2.01, 3.0]
    assert total == 6.02


def test_identity_helpers_reject_fractional_zero_and_boolean_ids():
    assert exact.positive_int(Decimal("42"), "product_id") == 42
    for invalid in (0, -1, Decimal("1.5"), True):
        with pytest.raises(ValueError):
            exact.positive_int(invalid, "product_id")
