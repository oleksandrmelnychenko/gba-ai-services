"""Exact decimal primitives for monetary values exposed by the solvency API."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

type MoneyValue = Decimal | int | float | str

CENT = Decimal("0.01")


def as_decimal(value: MoneyValue) -> Decimal:
    """Convert through the business representation and reject non-finite values."""
    if isinstance(value, bool):
        raise TypeError("boolean is not a monetary value")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"invalid monetary value: {value!r}") from exc
    if not result.is_finite():
        raise ValueError(f"monetary value must be finite: {value!r}")
    return result


def round_cent(value: MoneyValue) -> Decimal:
    """Round an aggregate once at the API boundary using accounting HALF_UP."""
    return as_decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)
