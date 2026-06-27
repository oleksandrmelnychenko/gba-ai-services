"""Application settings — env-only, no secrets in code (lesson from the prototype)."""
from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Database — read-only login required
    db_host: str = "127.0.0.1"
    db_port: int = 1433
    db_name: str = "ConcordDb_V5"
    db_user: str = "gba_reco_ro"
    db_password: str = Field(default="", description="Set in .env; never hardcode")
    db_pool_size: int = 10
    db_max_overflow: int = 10
    query_timeout: int = 25

    # Redis
    redis_host: str = "127.0.0.1"
    redis_port: int = 6379
    redis_db: int = 0
    cache_ttl: int = 3600
    feedback_ttl: int = 7776000

    # API
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    log_level: str = "INFO"
    # Shared secret the trusted gba-server proxy must present (X-Internal-Api-Key).
    # Empty = open (dev only); set in every non-local deployment.
    internal_api_key: str = ""
    # Browser origins allowed by CORS. Server-to-server callers send no Origin so this only
    # constrains browsers (defense-in-depth). Defaults to the gba-server/console origins.
    cors_allow_origins: list[str] = Field(
        default_factory=lambda: [
            "http://localhost:8083",
            "https://gba-console-dev.85.17.167.167.nip.io",
            "http://127.0.0.1:8083",
        ]
    )

    # Recommendation defaults
    default_top_n: int = 25
    repurchase_count: int = 20
    discovery_count: int = 5
    max_per_group: int = 3
    # Exclude products bought by more than this share of clients — universal staples / synthetic
    # accounting lines (e.g. "Ввід боргів" = debt entry, ~75% of clients). Real parts top out ~14%,
    # so 0.20 cleanly isolates synthetic lines. They are not cross-sell candidates and skew popularity.
    ubiquity_exclude_pct: float = 0.20
    # Synthetic accounting product(s) (e.g. debt-entry line 25422404) excluded unconditionally from
    # all recommendation/candidate populations, independent of the rolling ubiquity window. The
    # ubiquity threshold can drift below these depending on the 12-month window, so they are pinned.
    synthetic_product_ids: frozenset[int] = frozenset({25422404})
    # TTL (seconds) for the ubiquity set refresh — bounds staleness without process restart.
    ubiquity_cache_ttl: int = 3600

    # Cache-warming worker / scheduler
    # Look-back window (days) defining the "active client" set the worker precomputes for.
    active_client_days: int = 365
    # TTL (seconds) for warmed reco:* entries — must outlive the daily warm interval so a
    # warmed client stays a cache hit until the next refresh (8 days = 7-day window + slack).
    warm_cache_ttl: int = 691200
    # Hour (local tz) the daily warm job fires; mirrors gba-nba's daily-generate cron.
    daily_warm_hour: int = 3
    timezone: str = "Europe/Kyiv"

    @property
    def sqlalchemy_url(self) -> str:
        return (
            f"mssql+pymssql://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
