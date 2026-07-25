"""Exact numeric boundaries shared across product data and analytics.

SQL returns ``Decimal`` for all load-bearing aggregates. Calculations stay Decimal until the
public JSON boundary, where values are explicitly quantized with accounting ``ROUND_HALF_UP``.
The final ``float`` is only a JSON number transport type; no monetary calculation uses it.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation, localcontext
from math import isfinite
from typing import Any

MONEY_QUANTUM = Decimal("0.01")
UNIT_PRICE_QUANTUM = Decimal("0.0001")
QUANTITY_QUANTUM = Decimal("0.0001")
RATIO_QUANTUM = Decimal("0.0001")
COVER_DAYS_QUANTUM = Decimal("0.1")


def decimal_value(
    value: Any,
    field: str,
    *,
    default: Decimal | None = None,
    non_negative: bool = False,
) -> Decimal:
    """Parse a finite number without passing through binary float arithmetic."""
    if value is None:
        if default is not None:
            return default
        raise ValueError(f"{field} is required")
    if isinstance(value, bool):
        raise ValueError(f"{field} must be numeric")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be numeric") from exc
    if not number.is_finite():
        raise ValueError(f"{field} must be finite")
    if non_negative and number < 0:
        raise ValueError(f"{field} must be non-negative")
    return number


def quantized_float(
    value: Any,
    quantum: Decimal,
    field: str,
    *,
    non_negative: bool = False,
) -> float:
    """Quantize a finite Decimal with accounting rounding and return a JSON-safe number."""
    number = decimal_value(value, field, non_negative=non_negative)
    with localcontext() as context:
        context.prec = 50
        try:
            rounded = number.quantize(quantum, rounding=ROUND_HALF_UP)
        except InvalidOperation as exc:
            raise ValueError(f"{field} is outside the supported range") from exc
    result = float(rounded)
    if not isfinite(result):
        raise ValueError(f"{field} is outside the supported range")
    return result


def money(value: Any, field: str = "money", *, non_negative: bool = True) -> float:
    return quantized_float(value, MONEY_QUANTUM, field, non_negative=non_negative)


def unit_price(value: Any, field: str = "unit_price", *, non_negative: bool = True) -> float:
    return quantized_float(value, UNIT_PRICE_QUANTUM, field, non_negative=non_negative)


def quantity(value: Any, field: str = "quantity", *, non_negative: bool = True) -> float:
    return quantized_float(value, QUANTITY_QUANTUM, field, non_negative=non_negative)


def ratio(value: Any, field: str = "ratio", *, non_negative: bool = False) -> float:
    return quantized_float(value, RATIO_QUANTUM, field, non_negative=non_negative)


def cover_days(value: Any, field: str = "cover_days") -> float:
    return quantized_float(value, COVER_DAYS_QUANTUM, field, non_negative=True)


def decimal_sum(values: list[Any], field: str, *, non_negative: bool = False) -> Decimal:
    total = Decimal("0")
    for value in values:
        total += decimal_value(value, field, non_negative=non_negative)
    return total


def positive_int(value: Any, field: str) -> int:
    number = decimal_value(value, field)
    if number <= 0 or number != number.to_integral_value():
        raise ValueError(f"{field} must be a positive integer")
    return int(number)


def non_negative_int(value: Any, field: str) -> int:
    number = decimal_value(value, field)
    if number < 0 or number != number.to_integral_value():
        raise ValueError(f"{field} must be a non-negative integer")
    return int(number)
