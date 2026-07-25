from __future__ import annotations

from datetime import date

import pytest

from app.core.history import coverage, require_supported_as_of, subtract_months


def test_as_of_before_source_history_start_is_rejected():
    with pytest.raises(ValueError, match="2025-01-01"):
        require_supported_as_of("2024-12-31")


def test_window_crossing_floor_is_clamped_and_marked_incomplete():
    result = coverage("2025-06-30", 12)

    assert result.requested_start == date(2024, 6, 30)
    assert result.effective_start == date(2025, 1, 1)
    assert result.history_complete is False


def test_full_window_after_floor_preserves_requested_start():
    result = coverage("2026-07-25", 12)

    assert result.requested_start == date(2025, 7, 25)
    assert result.effective_start == date(2025, 7, 25)
    assert result.history_complete is True


def test_subtract_months_clamps_end_of_month():
    assert subtract_months(date(2025, 3, 31), 1) == date(2025, 2, 28)
