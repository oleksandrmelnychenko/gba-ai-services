"""Settings — env-only, no secrets in code."""
from __future__ import annotations

from functools import lru_cache

from pydantic import Field
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

    redis_host: str = "127.0.0.1"
    redis_port: int = 6379
    redis_db: int = 2
    cache_ttl: int = 3600

    api_host: str = "0.0.0.0"
    api_port: int = 8005
    log_level: str = "INFO"

    # Shared secret the trusted gba-server proxy must present (X-Internal-Api-Key).
    # Empty = open (dev only); set in every non-local deployment.
    internal_api_key: str = ""
    cors_allow_origins: list[str] = Field(
        default_factory=lambda: [
            "http://localhost:8083",
            "https://gba-console-dev.85.17.167.167.nip.io",
            "http://127.0.0.1:8083",
        ]
    )

    # bump on any scoring/classification change so outcomes can be sliced/A-B'd by version
    model_version: str = "products-v2-abc"

    # Inventory-health policy (env-tunable; calibrate thresholds on real data — never guess).
    velocity_window_days: int = 180     # trailing window for the demand rate
    dead_window_days: int = 365         # zero sales in this window + on-hand stock => dead
    seasonal_lookback_days: int = 730   # 24mo: distinguish "never sold" vs "stopped selling"
    cover_overstock_days: float = 180.0  # days-of-cover above this => overstock
    cover_target_min_days: float = 15.0  # healthy band lower bound
    cover_understock_days: float = 7.0   # below this (with demand) => understock / reorder
    slow_max_annual_units: float = 5.0   # <= this many units sold / yr => slow mover
    return_window_days: int = 365        # window for return-rate
    returns_high_min_rate: float = 0.05  # /assortment/returns default high-return threshold

    # Classification + health-score (Lens 1) — env-tunable, calibrate on real data.
    classify_months: int = 12            # monthly demand window for XYZ / trend / lifecycle
    trend_recent_months: int = 3         # recent vs prior split (kept for callers/back-compat)
    # XYZ variability method. "cv" = classic CV over the dense monthly grid (collapses to ~87% Z
    # under intermittent B2B demand); "interval" = CV of inter-demand intervals (Croston-style),
    # which measures cadence-regularity and discriminates far better on real data. Default interval.
    xyz_method: str = "interval"         # "cv" | "interval"
    xyz_x_cut: float = 0.3               # variability below this => X (regular/stable)
    xyz_y_cut: float = 0.6               # below this => Y, else Z (erratic/intermittent)
    abc_a_share: float = 0.80            # cumulative trailing revenue share boundary for A
    abc_b_share: float = 0.95            # ...and for B (rest = C)
    lifecycle_new_days: int = 90         # younger than this since first sale => new
    # Lifecycle trend window: recent N months vs the preceding N. Longer than the generic
    # trend split so a single quiet quarter does not read as decline; calibrated to 6.
    lifecycle_trend_months: int = 6
    lifecycle_growing_factor: float = 1.2  # recent rate >= prior * this => growing
    lifecycle_declining_factor: float = 0.5  # recent rate <= prior * this => declining
    # A SKU with NO demand in the trend window but sales earlier in the year is dormant, not
    # automatically declining. It only counts as DECLINING once its last sale is at least this
    # many months back (a genuine long fade); a shorter lull reads as MATURE.
    lifecycle_dormant_decline_months: int = 9
    margin_target: float = 0.30          # margin% that maps to a full health contribution
    return_rate_cap: float = 0.20        # return rate that maps to a zero health contribution
    # health-score component weights (normalized internally). Calibrated on 2025-06/09/12
    # snapshots against +180d demand/margin: ABC/trailing revenue carries the strongest repeat
    # demand signal, while returns are sparse and kept as a small quality penalty.
    w_stock: float = 0.25
    w_trend: float = 0.18
    w_margin: float = 0.20
    w_stability: float = 0.12
    w_returns: float = 0.03
    w_abc: float = 0.22

    @property
    def sqlalchemy_url(self) -> str:
        return (
            f"mssql+pymssql://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
