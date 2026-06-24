"""Application settings — env-only, no secrets in code (mirrors gba-solvency)."""
from __future__ import annotations

from datetime import date
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL


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

    # Redis — db 3 (0=reco, 1=procure, 2=solvency, 3=pricing)
    redis_host: str = "127.0.0.1"
    redis_port: int = 6379
    redis_db: int = 3
    cache_ttl: int = 3600
    redis_retry_cooldown_seconds: int = 30

    # API — port 8004 (8000 reco, 8001 procure, 8002 nba, 8003 solvency, 8004 pricing)
    api_host: str = "0.0.0.0"
    api_port: int = 8004
    log_level: str = "INFO"
    environment: str = "dev"
    # Shared secret the trusted gba-server proxy must present (X-Internal-Api-Key).
    # Empty = open (dev only); set in every non-local deployment.
    internal_api_key: str = ""
    # Browser origins permitted by CORS (defense-in-depth; server-to-server calls have no Origin).
    cors_allow_origins: list[str] = Field(
        default=[
            "http://localhost:8083",
            "https://gba-console-dev.85.17.167.167.nip.io",
            "http://127.0.0.1:8083",
        ]
    )

    # Pricing model (A+B: margin-floor + peer-price-band discount governor)
    model_version: str = "pricing-ab-v2"
    # Margin floor: recommended price never below unit_cost_eur*(1+target_margin_pct/100).
    target_margin_pct: float = 12.0
    # Peer band + cost lots are sampled over a trailing window by Sale.Created.
    trailing_window_months: int = 12

    # FX snapshot date — GetExchangedToEuroValue revalues at call time, so a run MUST pin a
    # fixed date to stay deterministic. Empty => the engine uses the request's as_of_date.
    fx_snapshot_date: str = ""

    # Synthetic 1С debt-entry line ('Ввід боргів з 1С'): contaminates cost lots and realized
    # price; EXCLUDED everywhere (cost, peer band, discount distribution).
    synthetic_line_product_id: int = 25422404

    # 1С debt/balance-import document type on dbo.ProductIncome.SourceDocumentType. These lots
    # carry inflated balance-import AccountingPrice (~800-26683 EUR) across BOTH
    # Consignment.IsImportedFromOneC and IsVirtual; EXCLUDED from the unit-cost median/fallback so
    # the margin floor is not contaminated. Real supply lots are SourceDocumentType IN (2,3).
    debt_import_source_document_type: int = 1

    # Peer-band UoM-outlier rejection (replaces the fixed bottom/top-decile trim). Realized
    # PricePerItem mixes piece-vs-box lines (e.g. a 0.34 piece price inside a 15-18 box band);
    # the rigid decile trim only removes 10% per tail and leaks when the contaminated fraction
    # exceeds that (verified: product 25104373 decile p75/p25=2.0 vs MAD 1.92; 25104980 decile
    # 1.53 vs MAD 1.03). A median/MAD modified-z reject adapts to the contaminated fraction (the
    # median has a 50% breakdown point) and preserves legitimate spread on clean products
    # (25300863's genuine 2.56 spread survives; 25381012 keeps p75/p25=1.11). MAD on the RAW price
    # tracks log-MAD here and avoids LOG(0). k=3.5 = the Iglewicz-Hoaglin cutoff: it cleans the
    # worst mix while k>=4.0 lets 25104373's box/piece split back in. 1.4826 = the normal-
    # consistency factor so MAD estimates sigma.
    peer_band_mad_k: float = 3.5
    # MAD reject is skipped (keep-all) below this many realized lines and whenever MAD<=0 (a
    # degenerate all-equal set) so a thin/tied product is never over-trimmed.
    peer_band_mad_min_rows: int = 4

    # Price-elasticity (SECONDARY signal; option C). Per-SKU constant-elasticity demand fit
    # (ln Q ~ -e ln price + agreement FE + month FE) for the estimable bands A/B, ProductGroup
    # pooling for sparser SKUs, NO estimate for the rest. HELD by default: the offline backtest
    # (scripts/elasticity_backtest.py) showed ~85% of observational fits are wrong-signed
    # (agreement-size + secular-drift confounding the data cannot break without experiments), so
    # only economically-sane fits (e in [lo,hi]) are ever surfaced and the elastic price NEVER
    # replaces the A+B recommended_price. elasticity_enabled gates the whole panel fetch+fit so it
    # can be turned on per-deployment once/if validation improves.
    elasticity_enabled: bool = False
    # Per-SKU fit requires at least this many valid lines in the window (bands A/B threshold=100);
    # below it the SKU borrows the ProductGroup pooled fit if that clears elasticity_pooled_min_lines.
    elasticity_min_lines_sku: int = 100
    elasticity_pooled_min_lines: int = 300
    # Economic-sanity band on e (own-price elasticity, positive for a normal good). Outside this an
    # estimate is treated as a sparse-data failure and suppressed to None.
    elasticity_sane_lo: float = 0.5
    elasticity_sane_hi: float = 5.0
    # Cap on products pulled into a ProductGroup pooled panel (highest-volume first).
    elasticity_pooled_max_products: int = 400

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

    def resolve_fx_date(self, as_of_date: str | None) -> str:
        if self.fx_snapshot_date:
            return self.fx_snapshot_date
        return as_of_date or date.today().isoformat()


@lru_cache
def get_settings() -> Settings:
    return Settings()
