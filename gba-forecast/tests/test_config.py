from __future__ import annotations

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
