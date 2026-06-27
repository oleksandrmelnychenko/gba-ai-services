"""Settings — env-only, no secrets in code."""
from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # MongoDB (task state)
    mongo_uri: str = Field(default="mongodb://127.0.0.1:27017", description="Set in .env")
    mongo_db: str = "gba_nba"

    # ConcordDb_V5 (read-only signals)
    db_host: str = "127.0.0.1"
    db_port: int = 1433
    db_name: str = "ConcordDb_V5"
    db_user: str = "gba_reco_ro"
    db_password: str = ""
    db_pool_size: int = 10
    db_max_overflow: int = 10

    # downstream AI services
    reco_url: str = "http://127.0.0.1:8000"
    reco_api_key: str = ""  # X-Internal-Api-Key for reco; required when reco enforces a key
    procure_url: str = "http://127.0.0.1:8001"
    http_timeout: int = 30
    reco_crosssell_timeout: int = 8   # short per-call bound so a slow reco can't stall daily generation

    # API. Host stays 0.0.0.0 for container networking; the security boundary is internal_api_key
    # plus not publishing the port externally — NOT the bind address.
    api_host: str = "0.0.0.0"
    api_port: int = 8002
    log_level: str = "INFO"

    # Daily generation schedule — every manager has a fresh inbox before they log in.
    timezone: str = "Europe/Kyiv"
    daily_generate_hour: int = 9   # 09:00 local time (Mon–Sat handled by working-day pace, not here)
    # Shared secret the trusted gba-server proxy must present (X-Internal-Api-Key).
    # Empty = open (dev only); set in every non-local deployment.
    internal_api_key: str = ""

    # NBA policy / throttling
    max_active_tasks_per_manager: int = 50
    max_tasks_per_client_per_day: int = 2
    task_ttl_days: int = 14
    dismiss_mute_days: int = 30
    service_level_due_days: int = 3
    crit_debt_reserve: int = 5

    # Signal tuning (calibrated 2026-06 on ConcordDb_V5 real data)
    debt_max_age_days: int = 365         # ignore stale write-off debts older than this (Debt.Created)
    debt_min_amount: float = 10.0        # EUR; drop settled/rounding sub-€10 overdues (~25% are noise)
    reorder_min_cycle_days: int = 7      # floor for span/(n-1) cycle, suppresses burst-buyer false-criticals
    reorder_max_overdue_mult: float = 3.0  # cap elapsed at N×cycle; beyond this the product is abandoned
    # cross_sell: only query reco for ACTIVE clients (reco needs history; cold clients yield no
    # discovery anyway). On real data this is ~20% of a manager's book — a 4-5× cut in reco calls.
    cross_sell_recent_days: int = 120
    cross_sell_min_orders: int = 3
    cross_sell_max_clients: int = 40  # only the top-N active clients by turnover get a (costly) reco call
    # Exclude products bought by >this share of clients — synthetic accounting lines ("Ввід боргів"
    # = debt entry, ~75% of clients) that aren't real products. Real parts top out ~14%.
    ubiquity_exclude_pct: float = 0.20
    # Synthetic accounting product(s) (e.g. debt-entry line 25422404 "Ввід боргів з 1С") excluded
    # UNCONDITIONALLY from turnover/feature signals, independent of the rolling ubiquity window.
    # 25422404 is today the only product clearing ubiquity_exclude_pct (~0.77), so it would be the
    # only one silently re-absorbed if its 12-month ubiquity ever dipped below the threshold — pin
    # it here so the exclusion is a hard guard, not a side effect of the data-driven ubiquity set.
    # Mirrors gba-reco/gba-products' synthetic-id hard exclusion.
    synthetic_product_ids: frozenset[int] = frozenset({25422404})

    # Scoring / model knobs — tunable WITHOUT redeploy (env-driven); bump model_version on change so
    # outcomes can be sliced/A-B'd by scoring generation.
    model_version: str = "nba-v3-propensity"  # priority now = 100*p_outcome from the trained model
    w_urgency: float = 0.5
    w_value: float = 0.3
    w_confidence: float = 0.2
    value_saturation: float = 6000.0          # EUR; calibrated to active-manager client annual monetary p75
    max_pace_boost: float = 1.25              # priority lift at 100% behind monthly pace
    target_trailing_months: int = 3           # run-rate window for the monthly minimum target
    urgency_band_critical: float = 0.85
    urgency_band_high: float = 0.6
    urgency_band_normal: float = 0.3

    # Feedback learning loop: repeatedly DISMISSED / done-not-sold (client, task_type) pairs get a
    # priority penalty so the queue learns from the manager's behaviour (beyond the 30d dismiss mute).
    feedback_window_days: int = 90
    feedback_penalty_per_rejection: float = 0.15   # priority ×(1 - 0.15·rejections)
    feedback_penalty_floor: float = 0.5            # never sink a task below half its score

    @property
    def sqlalchemy_url(self) -> str:
        return (
            f"mssql+pymssql://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
