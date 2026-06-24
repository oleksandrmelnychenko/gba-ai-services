"""Application settings — env-only, no secrets in code (mirrors gba-reco)."""
from __future__ import annotations

from datetime import date
from functools import lru_cache

from pydantic import AliasChoices, Field, field_validator
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

    # Redis — db 2 (0=reco, 1=procure, 2=solvency)
    redis_host: str = "127.0.0.1"
    redis_port: int = 6379
    redis_db: int = 2
    cache_ttl: int = 3600

    # API — port 8003 (8000 reco, 8001 procure, 8002 nba, 8003 solvency)
    api_host: str = "0.0.0.0"
    api_port: int = 8003
    log_level: str = "INFO"

    # Shared secret the trusted gba-server proxy must present (X-Internal-Api-Key).
    # Empty = open (dev only); set in every non-local deployment.
    internal_api_key: str = ""

    # Browser CORS allow-list (defense-in-depth; server-to-server calls carry no Origin so this
    # only constrains browsers). Defaults to the gba-server / gba-console origins.
    cors_allow_origins: list[str] = Field(
        default_factory=lambda: [
            "http://localhost:8083",
            "https://gba-console-dev.85.17.167.167.nip.io",
            "http://127.0.0.1:8083",
        ]
    )

    # Solvency model
    model_version: str = "creditscore100-v2"
    window_months: int = 12

    # FX snapshot date — GetExchangedToEuroValue revalues at call time, so a run MUST pin a
    # fixed date to stay deterministic. Empty => the engine uses the request's as_of_date.
    fx_snapshot_date: str = ""

    # Synthetic 1С debt-entry line ('Ввід боргів з 1С'): excluded from turnover/activity but
    # represents real carried debt, so it is KEPT in debt/exposure signals.
    synthetic_line_product_ids: set[int] = Field(
        default_factory=lambda: {25422404},
        validation_alias=AliasChoices("synthetic_line_product_ids", "synthetic_line_product_id"),
    )

    synthetic_line_product_name: str = "Ввід боргів"
    synthetic_drift_turnover_ratio: float = 10.0

    @field_validator("synthetic_line_product_ids", mode="before")
    @classmethod
    def _coerce_synthetic_ids(cls, v: object) -> object:
        if v is None or v == "":
            return {25422404}
        if isinstance(v, int):
            return {v}
        if isinstance(v, str):
            s = v.strip()
            if s.startswith("["):
                import json

                return {int(x) for x in json.loads(s)}
            return {int(part) for part in s.replace(";", ",").split(",") if part.strip()}
        return v

    # Sentinel order date used by 1С imports; excluded from tenure (MIN) computation.
    tenure_sentinel_date: str = "1980-01-01"

    @property
    def synthetic_line_product_id(self) -> int:
        return min(self.synthetic_line_product_ids)

    @property
    def sqlalchemy_url(self) -> str:
        return (
            f"mssql+pymssql://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )

    def resolve_fx_date(self, as_of_date: str | None) -> str:
        if self.fx_snapshot_date:
            return self.fx_snapshot_date
        return as_of_date or date.today().isoformat()


@lru_cache
def get_settings() -> Settings:
    return Settings()
