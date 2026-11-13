"""Settings — env-only, no secrets in code."""
from __future__ import annotations

from datetime import date
from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    source_environment: Literal["local", "shared", "dev", "qa"] = "shared"
    generation_enabled: bool = True
    warmers_enabled: bool = True

    @model_validator(mode="after")
    def validate_qa_isolation(self):
        if self.source_environment == "qa":
            if self.db_name != "ConcordDb_V5_QA" or self.mongo_db != "gba_nba_qa":
                raise ValueError("QA NBA requires its own SQL database and Mongo namespace")
            if self.generation_enabled:
                raise ValueError("QA generation must remain disabled until downstream AI sources are isolated")
        return self

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
    source_history_start_date: date = Field(
        default=date(2025, 1, 1),
        description="Earliest complete source date shared by all AI data paths",
    )

    # downstream AI services
    reco_url: str = "http://127.0.0.1:8000"
    reco_api_key: str = ""  # X-Internal-Api-Key for reco; required when reco enforces a key
    procure_url: str = "http://127.0.0.1:8001"
    http_timeout: int = 30
    reco_crosssell_timeout: int = 8   # short per-call bound so a slow reco can't stall daily generation

    # API. Host stays 0.0.0.0 for container networking; the security boundary is internal_api_key
    # plus not publishing the port externally — NOT the bind address.
    api_host: str = "127.0.0.1"
    api_port: int = 8002
    log_level: str = "INFO"

    # Daily generation schedule — every manager has a fresh inbox before they log in.
    timezone: str = "Europe/Kyiv"
    daily_generate_hour: int = 9   # 09:00 local time (Mon–Sat handled by working-day pace, not here)
    max_generation_lag_hours: int = Field(default=36, gt=0)
    max_source_lag_days: int = Field(default=31, gt=0)
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
    # Stale-queue retirement: an SLA-breached AI task nobody acted on is closed (superseded with
    # reason expired_stale) once its signal is gone or it is older than this many days from creation.
    stale_retirement_enabled: bool = True
    stale_task_max_age_days: int = Field(default=14, gt=0)

    # Signal tuning (calibrated 2026-06 on ConcordDb_V5 real data)
    debt_min_amount: float = 10.0        # EUR; drop settled/rounding sub-€10 overdues (~25% are noise)
    # Debt follow-up by collectability (docs/task-quality-2026-11-13.md): 66.1% of documents
    # 1-3 days past terms are settled within five days without a call, 5.6% of those over 90.
    # No task inside the grace period; past it the freshest debt is the most urgent.
    debt_grace_days: int = Field(default=3, ge=0)
    debt_fresh_max_days: int = Field(default=30, gt=0)
    debt_aged_max_days: int = Field(default=90, gt=0)
    reorder_min_cycle_days: int = 7      # floor for span/(n-1) cycle, suppresses burst-buyer false-criticals
    reorder_max_overdue_mult: float = 3.0  # cap elapsed at N×cycle; beyond this the product is abandoned
    # churn: a rate drop only counts once the silence exceeds the client's own rhythm.
    # Backtest 2026-01..2026-10 (docs/task-quality-2026-11-13.md): admitted churn tasks whose client
    # really stayed silent for 30 days went 49% -> 71% with max(30 days, 2 x own mean interval).
    churn_min_silence_days: int = Field(default=30, ge=0)
    churn_silence_cycle_mult: float = Field(default=2.0, ge=0)
    # cross_sell: only query reco for ACTIVE clients (reco needs history; cold clients yield no
    # discovery anyway). On real data this is ~20% of a manager's book — a 4-5× cut in reco calls.
    cross_sell_recent_days: int = 120
    cross_sell_min_orders: int = 3
    cross_sell_max_clients: int = 40  # only the top-N active clients by turnover get a (costly) reco call
    # Retrain backfill asks reco about every cross-sell client of every vintage; reco answers a
    # request in seconds, so a wide burst with a short timeout returns nothing.
    retrain_reco_workers: int = Field(default=4, ge=1, le=16)
    retrain_reco_timeout: int = Field(default=30, ge=1, le=300)
    # Exclude products bought by >this share of clients — synthetic accounting lines ("Ввід боргів"
    # = debt entry, ~75% of clients) that aren't real products. Real parts top out ~14%.
    ubiquity_exclude_pct: float = 0.20
    # Synthetic accounting product(s) (debt-entry «Ввід боргів») excluded UNCONDITIONALLY from
    # turnover/feature signals, independent of the rolling ubiquity window. Empty (the default) =
    # resolve the live id dynamically at runtime (signals_repository.synthetic_product_ids — the
    # dev DB re-mints product ids, so a pinned id goes stale, e.g. the dead 25422404); set via env
    # only as an explicit override. Mirrors gba-reco/gba-products' synthetic-id hard exclusion.
    synthetic_product_ids: frozenset[int] = frozenset()

    # Scoring / model knobs — tunable WITHOUT redeploy (env-driven); bump model_version on change so
    # outcomes can be sliced/A-B'd by scoring generation.
    model_version: str = "nba-v4-history-20250101"
    w_urgency: float = 0.5
    w_value: float = 0.3
    w_confidence: float = 0.2
    value_saturation: float = 6000.0          # EUR; calibrated to active-manager client annual monetary p75
    max_pace_boost: float = 1.25              # priority lift at 100% behind monthly pace
    target_trailing_months: int = 3           # run-rate window for the monthly minimum target
    urgency_band_critical: float = 0.85
    urgency_band_high: float = 0.6
    urgency_band_normal: float = 0.3
    # A task type whose model head has fewer labelled training rows than this (or failed the
    # retrain per-type gate) is ranked by the deterministic heuristic, not the model probability.
    min_model_training_rows: int = Field(default=500, ge=0)
    # The weekly retrain replaces the artefact; one older than this is reported as model_stale.
    model_max_age_days: int = Field(default=21, gt=0)
    # Expected value is the EUR expected inside this horizon — the model's outcome window.
    ev_horizon_days: int = Field(default=60, gt=0)

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


def require_generation_enabled() -> None:
    if not get_settings().generation_enabled:
        raise RuntimeError("generation_disabled_for_environment")
