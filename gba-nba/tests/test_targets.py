"""Sales-target engine tests — pure date/run-rate math + compute_target (repo monkeypatched)."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

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
    # covered calendar months without rows have zero activity
    assert targets.run_rate({"2026-05": 200.0}, "2026-06", 3) == Decimal(200) / 3
    assert targets.run_rate({}, "2026-06", 3) == 0.0


def test_compute_target_shape_and_pace(monkeypatch):
    series = {"2026-03": 120.0, "2026-04": 222.0, "2026-05": 246.0, "2026-06": 40.0}
    monkeypatch.setattr(sig_module(), "monthly_shipped", lambda mid, since, asof: series)
    monkeypatch.setattr(sig_module(), "monthly_paid", lambda mid, since, asof: series)

    out = targets.compute_target(1, as_of="2026-06-08")
    assert out["month"] == "2026-06"
    assert out["working_days"] == 26
    assert out["working_days_elapsed"] == 7
    assert out["source_history_start"] == "2025-01-01"
    assert out["effective_start"] == "2026-03-01"
    assert out["history_complete"] is True
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


def test_run_rate_does_not_substitute_older_active_month_for_zero_month():
    series = {"2026-06": 300, "2026-07": 100, "2026-09": 100, "2026-10": 500}
    assert targets.run_rate(series, "2026-10", 3) == Decimal(200) / 3


def test_run_rate_excludes_unknown_and_partial_source_months(monkeypatch):
    monkeypatch.setattr(targets.history, "source_history_start", lambda: date(2025, 1, 15))
    # January is partial/unknown; February is fully covered but has zero activity.
    assert targets.run_rate({"2025-01": 900, "2025-03": 100}, "2025-04", 3) == 50
    assert targets.run_rate({"2025-01": 900}, "2025-02", 3) == 0


def test_run_rate_handles_year_boundary_and_short_covered_history(monkeypatch):
    monkeypatch.setattr(targets.history, "source_history_start", lambda: date(2025, 12, 1))
    assert targets.run_rate({"2025-11": 900, "2025-12": 100}, "2026-02", 3) == 50


def test_compute_target_fetches_exact_calendar_window(monkeypatch):
    calls = []
    def series(mid, since, asof):
        calls.append((mid, since, asof))
        return {"2026-07": 100, "2026-09": 100}
    monkeypatch.setattr(sig_module(), "monthly_shipped", series)
    monkeypatch.setattr(sig_module(), "monthly_paid", series)
    result = targets.compute_target(7, "2026-10-22", trailing_months=3)
    assert calls == [(7, "2026-07-01", "2026-10-23")] * 2
    assert result["shipped"]["target"] == 66.67
    assert result["paid"]["target"] == 66.67
