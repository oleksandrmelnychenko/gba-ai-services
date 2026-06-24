"""Settings — env-only, no secrets in code."""
from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    db_host: str = "127.0.0.1"
    db_port: int = 1433
    db_name: str = "ConcordDb_V5"
    db_user: str = "gba_reco_ro"
    db_password: str = Field(default="", description="Set in .env; never hardcode")
    db_pool_size: int = 10
    db_max_overflow: int = 10
    query_timeout: int = 25

    redis_host: str = "127.0.0.1"
    redis_port: int = 6379
    redis_db: int = 1
    cache_ttl: int = 3600
    redis_retry_cooldown_seconds: int = 30

    mongo_uri: str = ""
    mongo_db: str = "gba_nba"
    use_masters: bool = True
    use_feedback: bool = True
    feedback_min_samples: int = 5
    override_factor_min: float = 0.5
    override_factor_max: float = 1.5

    api_host: str = "0.0.0.0"
    api_port: int = 8001
    log_level: str = "INFO"
    environment: str = "dev"

    # Shared secret the trusted gba-server proxy must present (X-Internal-Api-Key).
    # Empty = open (dev only); set in every non-local deployment.
    internal_api_key: str = ""
    # Browser origins allowed by CORS (defense-in-depth; server-to-server calls have no Origin).
    cors_allow_origins: list[str] = Field(
        default_factory=lambda: [
            "http://localhost:8083",
            "https://gba-console-dev.85.17.167.167.nip.io",
            "http://127.0.0.1:8083",
        ]
    )

    # Replenishment policy
    service_level: float = 0.99
    forecast_horizon_days: int = 30
    history_days: int = 120
    default_lead_time_days: int = 30
    lead_time_min_days: int = 1
    lead_time_max_days: int = 120
    lead_time_min_samples: int = 3
    lead_time_cv: float = 0.5

    economic_service_level: bool = True
    min_margin_eur: float = 0.1
    milp_time_limit: int = 10
    holding_rate_annual: float = 0.25
    service_level_min: float = 0.50
    service_level_max: float = 0.99
    service_level_floor_a: float = 0.95
    service_level_floor_b: float = 0.85

    # Demand forecaster: 'moving_avg' (default), 'croston', or 'sba'.
    # Croston/SBA target intermittent B2B spare-parts demand (many zero-days).
    forecast_method: str = "moving_avg"
    # Smoothing constant for Croston/SBA size & interval updates (0<alpha<=1).
    croston_alpha: float = 0.1
    # Per-quadrant forecaster dispatch by XYZ class (Z->SBA, Y->EWMA, X->moving_avg).
    # Off by default: benchmarks as a marginal fill/cost trade vs moving_avg, not a clear
    # win on this data; the capability stays config-toggleable. XYZ still drives SL capping.
    per_quadrant_forecast: bool = False
    ewma_alpha: float = 0.35
    seasonality_enabled: bool = False
    seasonal_shrinkage_k: float = 4.0
    seasonal_min: float = 0.6
    seasonal_max: float = 1.6
    seasonal_min_months: int = 12

    # Scheduler — daily warm of the cart (and per-producer) cache.
    timezone: str = "Europe/Kyiv"
    cart_warm_hour: int = 6        # 06:00 local — cart key warm before the workday
    producer_warm_hour: int = 5    # 05:00 local — full per-producer pass before the cart warm

    @property
    def sqlalchemy_url(self) -> URL:
        return URL.create(
            "mssql+pymssql",
            username=self.db_user,
            password=self.db_password,
            host=self.db_host,
            port=self.db_port,
            database=self.db_name,
        )

    def assert_release_safe(self, service_name: str) -> None:
        is_local = self.environment.lower() in {"dev", "local", "test", "development"}
        if not is_local and not self.internal_api_key:
            raise RuntimeError(f"{service_name}: INTERNAL_API_KEY is required outside dev/local/test")


@lru_cache
def get_settings() -> Settings:
    return Settings()
