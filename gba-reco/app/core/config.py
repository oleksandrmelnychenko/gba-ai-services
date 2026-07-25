"""Application settings — env-only, no secrets in code (lesson from the prototype)."""
from __future__ import annotations

from datetime import date
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Shared AI-fleet source contract. All business history is tracked from this date.
    source_history_start_date: date = date(2025, 1, 1)

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
    # Earliest complete transactional history imported from 1C. Every recommendation,
    # training and evaluation query is bounded by this date; rolling windows are additionally
    # clamped to it.
    source_history_start_date: date = date(2025, 1, 1)
    # Health fails closed when the canonical valid-sales spine has not advanced for this long.
    max_source_lag_days: int = Field(default=31, gt=0)
    # Exclude products bought by more than this share of clients — universal staples / synthetic
    # accounting lines (e.g. "Ввід боргів" = debt entry, ~75% of clients). Real parts top out ~14%,
    # so 0.20 cleanly isolates synthetic lines. They are not cross-sell candidates and skew popularity.
    ubiquity_exclude_pct: float = 0.20
    # Synthetic accounting product override. Default EMPTY: catalog re-syncs re-mint the debt-entry
    # row under a new ID (25422404 → 29555414 → ...), so the live id is resolved by Name
    # («Ввід боргів») at runtime — sales_repository.synthetic_product_ids(). Set
    # SYNTHETIC_PRODUCT_IDS in the env only to pin explicit ids over the dynamic resolution.
    synthetic_product_ids: frozenset[int] = frozenset()
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
    # One-shot retry backoff after a failed warm run (base doubles per attempt, capped) — a
    # failed 03:00/catch-up warm must not leave the cache cold until tomorrow's cron.
    warm_retry_base_minutes: int = 10
    warm_retry_max_minutes: int = 60

    @property
    def sqlalchemy_url(self) -> str:
        return (
            f"mssql+pymssql://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
