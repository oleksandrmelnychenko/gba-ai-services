from __future__ import annotations

from datetime import date

import pytest

from app.core.config import Settings


def test_runtime_config_requires_internal_key_unless_open_mode_is_explicit():
    settings = Settings(
        _env_file=None,
        internal_api_key="",
        allow_open_internal_api=False,
        db_password="unused",
    )

    with pytest.raises(RuntimeError, match="INTERNAL_API_KEY is required"):
        settings.validate_runtime_configuration()


def test_runtime_config_allows_explicit_local_open_mode():
    settings = Settings(
        _env_file=None,
        internal_api_key="",
        allow_open_internal_api=True,
        db_password="unused",
    )

    settings.validate_runtime_configuration()


def test_runtime_config_rejects_default_horizon_above_cap():
    settings = Settings(
        _env_file=None,
        internal_api_key="secret",
        forecast_horizon_months=25,
        max_forecast_horizon_months=24,
        db_password="unused",
    )

    with pytest.raises(RuntimeError, match="FORECAST_HORIZON_MONTHS"):
        settings.validate_runtime_configuration()


def test_runtime_config_rejects_minimum_history_above_history_window():
    settings = Settings(
        _env_file=None,
        internal_api_key="secret",
        history_months=3,
        min_history_months=4,
        db_password="unused",
    )

    with pytest.raises(RuntimeError, match="MIN_HISTORY_MONTHS"):
        settings.validate_runtime_configuration()


def test_source_history_start_date_defaults_and_parses_iso_override():
    default = Settings(_env_file=None, db_password="unused")
    overridden = Settings(
        _env_file=None,
        db_password="unused",
        source_history_start_date="2025-02-03",
    )

    assert default.source_history_start_date == date(2025, 1, 1)
    assert overridden.source_history_start_date == date(2025, 2, 3)
