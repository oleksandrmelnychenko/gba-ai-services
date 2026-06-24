"""Domain models for the gba-pricing A+B engine.

Model A+B (margin-floor + peer-price-band discount governor): per product × client-agreement,
recommend a price/discount that PROTECTS MARGIN and stays within peer norms by ADJUSTING the
existing price engine's DiscountRate lever — never replacing dbo.GetCalculatedProductPrice*.

Contract is aligned with the future gba-server (.NET) DTOs and the console.
"""
from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class Confidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class DiscountBand(BaseModel):
    """The defensible discount window expressed in the engine's own DiscountRate lever (%).

    min_pct is 0 because negative discounts are not an engine lever. max_pct is the strictest
    available hard upper bound between the margin-floor-implied discount and the peer P90 cap
    from ProductGroupDiscount.DiscountRate within the actual pricing segment. target_pct = the
    final suggested discount that reproduces recommended_price, clamped into [min_pct, max_pct].
    DISPLAY-ONLY: this band never feeds recommended_price / suggested_discount_pct / margin.
    """
    min_pct: float
    target_pct: float
    max_pct: float


class PeerBand(BaseModel):
    """Realized EUR unit-price percentiles across distinct client-agreements for the product."""
    p25: float | None = None
    p50: float | None = None
    p75: float | None = None
    n: int = 0


class PriceRecommendation(BaseModel):
    product_id: int
    client_agreement_netuid: str
    currency: str = "EUR"
    baseline_price: float | None = Field(
        default=None, description="dbo.GetCalculatedProductPriceWithSharesAndVat output, unchanged"
    )
    recommended_price: float | None = Field(
        default=None,
        description="final optimizer target; A+B target adjusted when the DiscountRate cap binds",
    )
    price_floor: float | None = Field(
        default=None, description="unit_cost_eur*(1+target_margin_pct/100); never recommend below"
    )
    unit_cost_eur: float | None = Field(
        default=None, description="robust per-product cost from ConsignmentItem.AccountingPrice"
    )
    suggested_discount_pct: float | None = Field(
        default=None, description="DiscountRate that reproduces recommended_price via the engine"
    )
    discount_band: DiscountBand | None = None
    peer_band: PeerBand = Field(default_factory=PeerBand)
    confidence: Confidence = Confidence.LOW
    margin_pct_at_recommended: float | None = None
    rationale: str = ""
    elasticity: float | None = Field(
        default=None,
        description="own-price elasticity e>0 (SECONDARY signal); None unless estimated AND "
        "economically sane on a high-data SKU. Observational panel FE -- never overrides A+B.",
    )
    elasticity_source: str | None = Field(
        default=None, description="per-sku | pooled-group | none -- provenance of the elasticity"
    )
    elastic_optimal_price: float | None = Field(
        default=None,
        description="cost*e/(e-1) markup-rule price (e>1); SECONDARY/advisory only. recommended_"
        "price stays the A+B value -- the elastic price is held until validated to win.",
    )
    as_of_date: str | None = None
    model_version: str = "pricing-ab-v2"


class PriceRequest(BaseModel):
    product_id: int | None = Field(default=None, description="dbo.Product.ID")
    product_net_uid: str | None = Field(default=None, description="dbo.Product.NetUID alternative")
    client_agreement_net_uid: str = Field(..., description="dbo.ClientAgreement.NetUID")
    culture: str = "uk"
    with_vat: bool = True
    target_margin_pct: float | None = Field(
        default=None, ge=0.0, le=100.0, description="override the config default margin floor"
    )
    use_cache: bool = True
    as_of_date: str | None = None


class BatchPriceItem(BaseModel):
    product_id: int | None = None
    product_net_uid: str | None = None
    client_agreement_net_uid: str


class BatchPriceRequest(BaseModel):
    items: list[BatchPriceItem] = Field(..., min_length=1, max_length=500)
    culture: str = "uk"
    with_vat: bool = True
    target_margin_pct: float | None = Field(default=None, ge=0.0, le=100.0)
    use_cache: bool = True
    as_of_date: str | None = None
