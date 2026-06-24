"""Always-on source guards (no DB) — pin the just-fixed currency-conversion SQL into CI.

Every mocked unit test stayed GREEN while a real currency bug shipped (UAH summed as if EUR,
~50x inflated), because mocks never see the SQL. These guards read the repository module source
with inspect.getsource and assert the fixed conversion patterns are present and the reverted
(raw-sum) patterns are absent — so reintroducing the bug fails CI immediately, no DB required.
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

from app.data import signals_repository as sig
from scripts import realdata_census

_OVERDUE_SRC = inspect.getsource(sig.overdue_debts_for_manager)
_PAID_SRC = inspect.getsource(sig.monthly_paid)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s)


def test_overdue_debt_wraps_total_in_euro_conversion():
    norm = _norm(_OVERDUE_SRC)
    assert "GetExchangedToEuroValue(d.Total" in norm, \
        "overdue debt must convert Debt.Total to EUR via dbo.GetExchangedToEuroValue"
    assert "ISNULL(a.CurrencyID, 2)" in norm, \
        "conversion must default missing agreement currency to EUR (2), not assume the debt is EUR"


def test_overdue_debt_does_not_raw_sum_total():
    norm = _norm(_OVERDUE_SRC)
    assert "SUM(d.Total)" not in norm, \
        "raw SUM(d.Total) treats UAH debt as EUR (~50x inflated) — must stay wrapped in conversion"
    assert re.search(r"SUM\(\s*d\.Total\s*\)", _OVERDUE_SRC) is None, \
        "no raw SUM over Debt.Total in any whitespace form"


def test_monthly_paid_converts_amount_not_euroamount():
    norm = _norm(_PAID_SRC)
    assert "GetExchangedToEuroValue(p.Amount" in norm, \
        "monthly_paid must convert the local Amount to EUR (EuroAmount is unreliable on this data)"


def test_monthly_paid_does_not_trust_euroamount():
    norm = _norm(_PAID_SRC)
    assert "p.EuroAmount" not in norm, \
        "p.EuroAmount is ~16-23x too high for UAH payments — must not be used"
    assert "SUM(p.EuroAmount)" not in norm, \
        "summing EuroAmount reintroduces the inflated-paid bug"


def test_overdue_debt_uses_parameterized_filters():
    assert ":mid" in _OVERDUE_SRC and ":asof" in _OVERDUE_SRC
    assert ":maxage" in _OVERDUE_SRC and ":minamt" in _OVERDUE_SRC


def test_realdata_census_has_fail_fast_check_mode():
    src = inspect.getsource(realdata_census)
    assert "--check" in src
    assert "total_candidates" in src
    assert "return 1" in src


def test_makefile_exposes_calibration_target():
    makefile = Path(__file__).resolve().parents[1] / "Makefile"
    src = makefile.read_text(encoding="utf-8")
    assert "calibration:" in src
    assert "realdata_census --check" in src
