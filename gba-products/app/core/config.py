"""Settings — env-only, no secrets in code."""
from __future__ import annotations

from datetime import date
from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    source_environment: Literal["local", "dev", "qa"] = "dev"
    cache_namespace: str = "products"
    warmers_enabled: bool = True

    @model_validator(mode="after")
    def validate_source_isolation(self):
        if self.source_environment == "qa":
            if (self.db_name, self.redis_db, self.cache_namespace) != ("ConcordDb_V5_QA", 12, "products-qa"):
                raise ValueError("QA products requires isolated SQL, Redis DB and cache namespace")
            if self.warmers_enabled:
                raise ValueError("QA products background warmers must remain disabled")
            if not self.internal_api_key or self.db_user.lower() == "sa":
                raise ValueError("QA products requires a private API key and read-only SQL identity")
        elif self.source_environment == "dev":
            if (self.db_name, self.redis_db, self.cache_namespace) != ("ConcordDb_V5", 2, "products"):
                raise ValueError("DEV products source configuration must not use another environment")
        return self

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
    # ~25h: the day's snapshots must outlive the whole business day; freshness comes from the
    # hourly in-process portfolio refresh, not from TTL expiry (a 1h TTL made the dashboard
    # rebuild cold every hour).
    cache_ttl: int = 90000

    # The product-page facts (product x month, product x region) cover closed days only, so they
    # change during a day only through backdated or late-imported documents. They are built once
    # per cache generation and rebuilt when older than this, which bounds that staleness without
    # repeating the two catalog-wide passes (about 36 s of SQL together) every hour.
    product_facts_refresh_seconds: int = 21600

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
    # Stock semantics changed from the now-empty ReSaleAvailability cost layer to operational
    # ProductAvailability, and v4 makes all currency/quantity boundaries Decimal + HALF_UP.
    # v5 adds the factual source-history floor and invalidates every pre-floor dense/cache outcome.
    # v7 adds the minimum-volume rule to growing/declining lifecycle labels.
    # v8 compares the lifecycle halves as daily rates (the as-of month is partial).
    # v9 dates the first sale over the whole factual history, so returning SKUs are not "new".
    # v10 grades XYZ by demand frequency instead of the inter-demand interval CV.
    model_version: str = "products-v10-confirmed-sales-kyiv"

    # Earliest factually available historical source date. All rolling SQL, dense grids,
    # denominators, producer lookups, API metadata, and cache epochs are clamped to this floor.
    source_history_start_date: date = date(2025, 1, 1)

    # Optional override for the synthetic «Ввід боргів» debt-entry ProductID. 0 = resolve it
    # dynamically from dbo.Product (the 1С sync re-mints the row, so a pinned ID goes stale).
    synthetic_product_id: int = 0

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
    # XYZ method. "frequency" (default) = share of observed months with demand: on 8 rolling
    # origins it orders SKUs by the chance of a sale in the next 90 days (X 94%, Y 72%, Z 36%).
    # "interval" = CV of inter-demand intervals: a SKU sold in exactly two months has one gap,
    # CV 0, and was graded X (half of the old X class). "cv" = classic CV over the dense grid
    # (about 88% Z under intermittent demand). The two cuts below serve "interval" and "cv" only.
    xyz_method: Literal["frequency", "interval", "cv"] = "frequency"
    xyz_x_min_share: float = 0.75        # demand in at least this share of months => X
    xyz_y_min_share: float = 0.33        # ...at least this share => Y, else Z
    xyz_x_cut: float = 0.3               # variability below this => X (regular/stable)
    xyz_y_cut: float = 0.6               # below this => Y, else Z (erratic/intermittent)
    abc_a_share: float = 0.80            # cumulative trailing revenue share boundary for A
    abc_b_share: float = 0.95            # ...and for B (rest = C)
    lifecycle_new_days: int = 90         # younger than this since first sale => new
    # The first sale is looked up over this many months (clamped to the source-history floor),
    # not over the classification window: a SKU that sold more than a year ago and sells again
    # is a returning product, not a new one.
    lifecycle_first_sale_months: int = 60
    # Lifecycle trend window: recent N months vs the preceding N. Longer than the generic
    # trend split so a single quiet quarter does not read as decline; calibrated to 6.
    lifecycle_trend_months: int = 6
    lifecycle_growing_factor: float = 1.2  # recent rate >= prior * this => growing
    lifecycle_declining_factor: float = 0.5  # recent rate <= prior * this => declining
    # Growing/declining are trend claims and need volume behind them: recent + prior units over
    # the two compared windows must reach this floor, otherwise a selling SKU is MATURE. Under
    # "no trend" each unit lands in either half with p=0.5, so all of 6 units falling into the
    # recent half has probability 1.6% (5 units: 3.1%, 2 units: 25% — the old "growing" noise).
    lifecycle_min_trend_units: float = 6.0
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
