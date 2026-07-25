"""Unit contract for the shared 1C transactional-history boundary."""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from app.core.config import Settings
from app.core.history import (
    full_history_coverage,
    parse_as_of,
    require_supported_as_of,
    source_history_start_iso,
)


def test_default_source_history_start_is_2025_01_01():
    assert Settings.model_fields["source_history_start_date"].default == date(2025, 1, 1)
    assert source_history_start_iso() == "2025-01-01"


def test_env_template_documents_source_history_start():
    template = Path(".env.example").read_text()
    assert "SOURCE_HISTORY_START_DATE=2025-01-01" in template


def test_history_boundary_accepts_exact_date_and_timestamp():
    assert require_supported_as_of("2025-01-01") == date(2025, 1, 1)
    assert require_supported_as_of("2025-01-01 12:34:56") == date(2025, 1, 1)
    assert parse_as_of("2026-07-25T12:00:00Z") == date(2026, 7, 25)


def test_history_boundary_rejects_day_before_floor():
    with pytest.raises(ValueError, match="source history start 2025-01-01"):
        require_supported_as_of("2024-12-31 23:59:59")


def test_full_history_metadata_is_explicit_and_complete():
    coverage = full_history_coverage("2026-07-25")

    assert coverage.source_history_start == date(2025, 1, 1)
    assert coverage.effective_start == date(2025, 1, 1)
    assert coverage.history_complete is True
