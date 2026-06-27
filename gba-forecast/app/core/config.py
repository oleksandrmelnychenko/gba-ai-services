"""Settings — env-only, no secrets in code."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    db_host: str = "127.0.0.1"
    db_port: int = 1433
    db_name: str = "ConcordDb_V5"
    db_user: str = "gba_reco_ro"
    db_password: str = Field(default="", description="Set in .env; never hardcode")
    db_pool_size: int = 10
    db_max_overflow: int = 10
    db_login_timeout_seconds: int = 5
    db_query_timeout_seconds: int = 30

    redis_host: str = "127.0.0.1"
    redis_port: int = 6379
    redis_db: int = 3
    cache_ttl: int = 3600
    redis_retry_interval_seconds: int = 30

    api_host: str = "0.0.0.0"
    api_port: int = 8006
    log_level: str = "INFO"

    # Shared secret the trusted gba-server proxy must present (X-Internal-Api-Key).
    # Empty is allowed only when ALLOW_OPEN_INTERNAL_API=true (local/dev only).
    internal_api_key: str = ""
    allow_open_internal_api: bool = False
    cors_allow_origins: list[str] = Field(
        default_factory=lambda: [
            "http://localhost:8083",
            "https://gba-console-dev.85.17.167.167.nip.io",
            "http://127.0.0.1:8083",
        ]
    )

    # bump on any forecast-method change so outcomes can be sliced/A-B'd by version
    model_version: str = "forecast-v1"

    # Forecast policy (env-tunable; calibrate on real data — never guess).
    forecast_horizon_months: int = 6  # default # of months to project forward
    max_forecast_horizon_months: int = 24  # hard API cap to prevent accidental heavy calls
    history_months: int = 24  # trailing window of monthly history fed to the model
    # Mode selecting how the monthly EUR sale series is projected forward:
    #   "auto"        — per-series method selection by Syntetos-Boylan demand quadrant
    #                   (smooth -> EWMA, erratic -> moving_avg, intermittent/lumpy -> SBA);
    #                   default, backtest-validated (on the corrected full-sales data) as
    #                   lower-error than any single fixed method on 1000 real series.
    #   "moving_avg"  — force the trailing-window mean for every series (legacy / A-B baseline)
    #   "croston"     — force Croston's intermittent-demand rate (size / interval)
    #   "sba"         — force Syntetos-Boylan bias-corrected Croston
    #   "ewma"        — force the recency-weighted EWMA level (trend-tracking; cures under-bias)
    #   "damped_trend"— force Holt damped-trend (level + damped slope; A-B only)
    forecast_method: str = "auto"  # "auto"|"moving_avg"|"croston"|"sba"|"ewma"|"damped_trend"
    croston_alpha: float = 0.1  # smoothing constant for croston/sba
    # A series with fewer than this many non-zero history months is "too thin" — return []
    # for that key so the console shows «немає даних» instead of a bogus flat forecast.
    # 1 point is pure noise (no rate, no cadence); 3 is the floor at which Croston/SBA can
    # even form an inter-demand interval and the window mean is not a single observation.
    min_history_months: int = 3

    @field_validator(
        "db_pool_size",
        "db_login_timeout_seconds",
        "db_query_timeout_seconds",
        "redis_retry_interval_seconds",
        "forecast_horizon_months",
        "max_forecast_horizon_months",
        "history_months",
        "min_history_months",
    )
    @classmethod
    def positive_int(cls, value: int) -> int:
        if value < 1:
            raise ValueError("must be greater than 0")
        return value

    @field_validator("db_max_overflow", "cache_ttl")
    @classmethod
    def non_negative_int(cls, value: int) -> int:
        if value < 0:
            raise ValueError("must be greater than or equal to 0")
        return value

    @field_validator("croston_alpha")
    @classmethod
    def croston_alpha_between_zero_and_one(cls, value: float) -> float:
        if not 0 < value <= 1:
            raise ValueError("must be in (0, 1]")
        return value

    def validate_runtime_configuration(self) -> None:
        if not self.internal_api_key and not self.allow_open_internal_api:
            raise RuntimeError(
                "INTERNAL_API_KEY is required. Set ALLOW_OPEN_INTERNAL_API=true only for local/dev."
            )

        if self.forecast_horizon_months > self.max_forecast_horizon_months:
            raise RuntimeError("FORECAST_HORIZON_MONTHS cannot exceed MAX_FORECAST_HORIZON_MONTHS")

    @property
    def sqlalchemy_url(self) -> str:
        return (
            f"mssql+pymssql://{self.db_user}:{self.db_password}@{self.db_host}:{self.db_port}/{self.db_name}"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
