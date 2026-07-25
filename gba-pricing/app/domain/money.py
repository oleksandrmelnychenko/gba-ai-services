"""Exact decimal primitives for EUR values used by the pricing API.

SQL Server decimals and user-configured percentages must never enter accounting
calculations through binary ``float`` arithmetic.  Conversion from legacy/test
floats intentionally goes through ``str`` so the represented business value is
preserved (for example ``1.005`` stays exactly ``Decimal("1.005")``).
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

type MoneyValue = Decimal | int | float | str

CENT = Decimal("0.01")
HUNDRED = Decimal("100")
ZERO = Decimal("0")


def as_decimal(value: MoneyValue) -> Decimal:
    """Return a finite Decimal without importing binary-float representation noise."""
    if isinstance(value, bool):
        raise TypeError("boolean is not a monetary value")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"invalid decimal value: {value!r}") from exc
    if not result.is_finite():
        raise ValueError(f"decimal value must be finite: {value!r}")
    return result


def optional_decimal(value: MoneyValue | None) -> Decimal | None:
    return None if value is None else as_decimal(value)


def round_cent(value: MoneyValue) -> Decimal:
    """Accounting rounding used at every EUR API boundary."""
    return as_decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)
