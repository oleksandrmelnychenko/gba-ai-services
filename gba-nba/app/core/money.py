"""Accounting-safe money helpers for API/dashboard boundaries."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

CENT = Decimal("0.01")
TENTH = Decimal("0.1")
ZERO = Decimal("0")


def decimal_value(value: object | None) -> Decimal:
    if value is None:
        return ZERO
    if isinstance(value, Decimal):
        result = value
    else:
        try:
            result = Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise ValueError("money value must be numeric") from exc
    if not result.is_finite():
        raise ValueError("money value must be finite")
    return result


def cents_decimal(value: object | None) -> Decimal:
    return decimal_value(value).quantize(CENT, rounding=ROUND_HALF_UP)


def cents(value: object | None) -> float:
    """JSON-compatible number rounded with accounting ROUND_HALF_UP."""
    return float(cents_decimal(value))


def tenths(value: object | None) -> float:
    return float(decimal_value(value).quantize(TENTH, rounding=ROUND_HALF_UP))
