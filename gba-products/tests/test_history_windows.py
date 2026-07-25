from __future__ import annotations

from datetime import date

import pytest

from app.core.config import Settings
from app.core.history import (
    combined_history_metadata,
    day_history_window,
    month_history_window,
)


def test_source_history_floor_defaults_and_parses_iso_override():
    default = Settings(_env_file=None, db_password="unused")
    overridden = Settings(
        _env_file=None,
        db_password="unused",
        source_history_start_date="2025-02-03",
    )

    assert default.source_history_start_date == date(2025, 1, 1)
    assert overridden.source_history_start_date == date(2025, 2, 3)


def test_day_window_clamps_and_reports_effective_denominator():
    window = day_history_window("2025-01-11", 180, date(2025, 1, 1))

    assert window.requested_start == date(2024, 7, 15)
    assert window.effective_start == date(2025, 1, 1)
    assert window.effective_days == 10
    assert window.history_complete is False


def test_day_window_is_complete_once_requested_start_is_after_floor():
    window = day_history_window("2026-07-25", 180, date(2025, 1, 1))

    assert window.requested_start == window.effective_start == date(2026, 1, 26)
    assert window.effective_days == 180
    assert window.history_complete is True


def test_month_window_uses_calendar_start_and_source_floor():
    window = month_history_window("2026-07-25", 24, date(2025, 1, 1))

    assert window.requested_start == date(2024, 8, 1)
    assert window.effective_start == date(2025, 1, 1)
    assert window.history_complete is False


def test_combined_metadata_discloses_each_signal_window():
    windows = {
        "velocity": day_history_window("2025-07-01", 180, date(2025, 1, 1)),
        "dead": day_history_window("2025-07-01", 365, date(2025, 1, 1)),
    }

    metadata = combined_history_metadata(windows)

    assert metadata["source_history_start"] == "2025-01-01"
    assert metadata["requested_start"] == "2024-07-01"
    assert metadata["effective_start"] == "2025-01-01"
    assert metadata["history_complete"] is False
    assert metadata["history_windows"]["velocity"]["effective_days"] == 180


def test_history_window_rejects_as_of_before_source_floor():
    with pytest.raises(ValueError, match="as_of_date_before_source_history_start"):
        day_history_window("2024-12-31", 30, date(2025, 1, 1))
