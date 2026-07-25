"""Domain models for the gba-pricing A+B engine.

Model A+B (margin-floor + peer-price-band discount governor): per product × client-agreement,
recommend a price/discount that PROTECTS MARGIN and stays within peer norms by ADJUSTING the
existing price engine's DiscountRate lever — never replacing dbo.GetCalculatedProductPrice*.

Contract is aligned with the future gba-server (.NET) DTOs and the console.
"""
from __future__ import annotations

from datetime import date
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator, model_validator

from app.domain.money import round_cent


class Confidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class DiscountBand(BaseModel):
    """The defensible discount window expressed in the engine's own DiscountRate lever (%).

    Edges = { floor-implied discount (most aggressive discount that still holds the margin floor),
    peer P90 cap from ProductGroupDiscount.DiscountRate within the segment }, emitted as
    min_pct <= max_pct (sorted) so the rendered range is always monotone. target_pct = the
    suggested discount that reproduces recommended_price, clamped into [min_pct, max_pct].
    DISPLAY-ONLY: this band never feeds recommended_price / suggested_discount_pct / margin.
    """
    min_pct: float = Field(ge=0.0, le=100.0, allow_inf_nan=False)
    target_pct: float = Field(ge=0.0, le=100.0, allow_inf_nan=False)
    max_pct: float = Field(ge=0.0, le=100.0, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_order(self) -> DiscountBand:
        if not self.min_pct <= self.target_pct <= self.max_pct:
            raise ValueError("discount band must satisfy min_pct <= target_pct <= max_pct")
        return self


class PeerBand(BaseModel):
    """Realized EUR unit-price percentiles across distinct client-agreements for the product."""

    p25: float | None = Field(default=None, ge=0.0, allow_inf_nan=False)
    p50: float | None = Field(default=None, ge=0.0, allow_inf_nan=False)
    p75: float | None = Field(default=None, ge=0.0, allow_inf_nan=False)
    n: int = Field(default=0, ge=0)

    @field_validator("p25", "p50", "p75", mode="before")
    @classmethod
    def round_money_percentiles(cls, value):
        return None if value is None else float(round_cent(value))

    @model_validator(mode="after")
    def validate_percentile_order(self) -> PeerBand:
        values = [value for value in (self.p25, self.p50, self.p75) if value is not None]
        if values != sorted(values):
            raise ValueError("peer price percentiles must be monotone")
        return self


class PriceRecommendation(BaseModel):
    product_id: int = Field(gt=0)
    product_net_uid: str | None = Field(
        default=None,
        description="canonical dbo.Product.NetUID echoed for end-to-end identity validation",
    )
    client_agreement_netuid: str = Field(min_length=1)
    currency: str = "EUR"
    baseline_price: float | None = Field(
        default=None,
        ge=0.0,
        allow_inf_nan=False,
        description="live/fallback baseline exposed at EUR-cent precision",
    )
    baseline_source: str | None = Field(
        default=None,
        description="'agreement' = live engine price; 'client_world_fallback' = median realized "
        "PricePerItem of the client's recent SAME-Organization-world sales (used when the "
        "agreement-scoped baseline is NULL, e.g. inactive/deleted Agreement); None = no baseline",
    )
    recommended_price: float | None = Field(
        default=None,
        ge=0.0,
        allow_inf_nan=False,
        description="optimizer target; clamp(max(floor,peer_P50), floor, baseline)",
    )
    price_floor: float | None = Field(
        default=None,
        ge=0.0,
        allow_inf_nan=False,
        description="unit_cost_eur*(1+target_margin_pct/100); never recommend below",
    )
    unit_cost_eur: float | None = Field(
        default=None,
        ge=0.0,
        allow_inf_nan=False,
        description="robust per-product cost from ConsignmentItem.AccountingPrice",
    )
    suggested_discount_pct: float | None = Field(
        default=None,
        ge=0.0,
        le=100.0,
        allow_inf_nan=False,
        description="DiscountRate that reproduces recommended_price via the engine",
    )
    discount_band: DiscountBand | None = None
    peer_band: PeerBand = Field(default_factory=PeerBand)
    confidence: Confidence = Confidence.LOW
    margin_pct_at_recommended: float | None = Field(default=None, allow_inf_nan=False)
    rationale: str = ""
    elasticity: float | None = Field(
        default=None,
        allow_inf_nan=False,
        description="own-price elasticity e>0 (SECONDARY signal); None unless estimated AND "
        "economically sane on a high-data SKU. Observational panel FE -- never overrides A+B.",
    )
    elasticity_source: str | None = Field(
        default=None, description="per-sku | pooled-group | none -- provenance of the elasticity"
    )
    elastic_optimal_price: float | None = Field(
        default=None,
        ge=0.0,
        allow_inf_nan=False,
        description="cost*e/(e-1) markup-rule price (e>1); SECONDARY/advisory only. recommended_"
        "price stays the A+B value -- the elastic price is held until validated to win.",
    )
    source_history_start: str | None = Field(
        default=None,
        description="first factual Sale.Created date available to behavioral pricing signals",
    )
    requested_start: str | None = Field(
        default=None,
        description="unclamped start of the configured trailing behavioral window",
    )
    effective_start: str | None = Field(
        default=None,
        description="actual behavioral window start after the factual-source floor is applied",
    )
    history_complete: bool | None = Field(
        default=None,
        description="true when the full requested trailing window exists in the source",
    )
    history_fingerprint: str | None = Field(
        default=None,
        description="stable namespace for the factual-history contract",
    )
    model_fingerprint: str | None = Field(
        default=None,
        description="serving model namespace including version, window and history floor",
    )
    as_of_date: str | None = None
    model_version: str = "pricing-ab-v2"

    @field_validator(
        "baseline_price",
        "recommended_price",
        "price_floor",
        "unit_cost_eur",
        "elastic_optimal_price",
        mode="before",
    )
    @classmethod
    def round_api_money(cls, value):
        return None if value is None else float(round_cent(value))


class PriceRequest(BaseModel):
    product_id: int | None = Field(default=None, gt=0, description="dbo.Product.ID")
    product_net_uid: str | None = Field(default=None, description="dbo.Product.NetUID alternative")
    client_agreement_net_uid: str = Field(
        ..., min_length=1, description="dbo.ClientAgreement.NetUID"
    )
    culture: str = Field(default="uk", min_length=1)
    with_vat: bool = True
    target_margin_pct: float | None = Field(
        default=None, ge=0.0, le=100.0, description="override the config default margin floor"
    )
    use_cache: bool = True
    as_of_date: date | None = None

    @field_validator("product_net_uid", "client_agreement_net_uid", "culture", mode="before")
    @classmethod
    def strip_identity_strings(cls, value):
        return value.strip() if isinstance(value, str) else value

    @model_validator(mode="after")
    def validate_product_identity(self) -> PriceRequest:
        if self.product_id is None and not self.product_net_uid:
            raise ValueError("product_id or product_net_uid required")
        return self


class BatchPriceItem(BaseModel):
    product_id: int | None = Field(default=None, gt=0)
    product_net_uid: str | None = None
    client_agreement_net_uid: str = Field(min_length=1)

    @field_validator("product_net_uid", "client_agreement_net_uid", mode="before")
    @classmethod
    def strip_identity_strings(cls, value):
        return value.strip() if isinstance(value, str) else value

    @model_validator(mode="after")
    def validate_product_identity(self) -> BatchPriceItem:
        if self.product_id is None and not self.product_net_uid:
            raise ValueError("product_id or product_net_uid required")
        return self


class BatchPriceRequest(BaseModel):
    items: list[BatchPriceItem] = Field(..., min_length=1, max_length=500)
    culture: str = Field(default="uk", min_length=1)
    with_vat: bool = True
    target_margin_pct: float | None = Field(default=None, ge=0.0, le=100.0)
    use_cache: bool = True
    as_of_date: date | None = None

    @field_validator("culture", mode="before")
    @classmethod
    def strip_culture(cls, value):
        return value.strip() if isinstance(value, str) else value
