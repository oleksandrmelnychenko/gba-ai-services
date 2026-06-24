"""Sales-target engine tests — pure date/run-rate math + compute_target (repo monkeypatched)."""
from __future__ import annotations

from datetime import date

from app.services import targets


def test_working_days_mon_to_sat():
    # June 2026: 30 days, Sundays = 7,14,21,28 (4) -> 26 working days
    assert targets.working_days_in_month(2026, 6) == 26
    # through Mon 2026-06-08: days 1..8 minus Sun the 7th -> 7
    assert targets.working_days_elapsed(date(2026, 6, 8)) == 7


def test_run_rate_uses_completed_months_only():
    series = {"2026-03": 120.0, "2026-04": 222.0, "2026-05": 246.0, "2026-06": 40.0}
    # current partial month (2026-06) excluded; avg of Mar/Apr/May
    assert targets.run_rate(series, "2026-06", 3) == (120 + 222 + 246) / 3
    # fewer completed than n -> average what exists
    assert targets.run_rate({"2026-05": 200.0}, "2026-06", 3) == 200.0
    assert targets.run_rate({}, "2026-06", 3) == 0.0


def test_compute_target_shape_and_pace(monkeypatch):
    series = {"2026-03": 120.0, "2026-04": 222.0, "2026-05": 246.0, "2026-06": 40.0}
    monkeypatch.setattr(sig_module(), "monthly_shipped", lambda mid, since, asof: series)
    monkeypatch.setattr(sig_module(), "monthly_paid", lambda mid, since, asof: series)

    out = targets.compute_target(1, as_of="2026-06-08")
    assert out["month"] == "2026-06"
    assert out["working_days"] == 26
    assert out["working_days_elapsed"] == 7
    sh = out["shipped"]
    assert sh["target"] == round((120 + 222 + 246) / 3, 2)   # 196.0
    assert sh["mtd"] == 40.0
    assert sh["gap"] > 0                                       # behind pace
    assert sh["pace_status"] == "behind"
    assert 0 < sh["attainment_pct"] < 100


def test_pace_status_bands():
    assert targets._pace_status(110, 100) == "ahead"
    assert targets._pace_status(100, 100) == "on"
    assert targets._pace_status(80, 100) == "behind"
    assert targets._pace_status(5, 0) == "on"   # no expectation yet


def sig_module():
    from app.data import signals_repository
    return signals_repository
