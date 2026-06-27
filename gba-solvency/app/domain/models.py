"""Domain models for the CreditScore-100 solvency engine.

Contract is aligned with the future gba-server (.NET) DTOs and the console charts.
"""
from __future__ import annotations

from enum import IntEnum, StrEnum

from pydantic import BaseModel, Field


class SalePaymentStatusType(IntEnum):
    """CONFIRMED enum for regular sales — Domain/EntityHelpers/Sales/SalePaymentStatusType.cs."""
    NotPaid = 0
    Paid = 1
    Overpaid = 2
    PartialPaid = 3
    Refund = 4


class RetailPaymentStatusType(IntEnum):
    """Retail sales use a DIFFERENT enum — Domain/EntityHelpers/Clients/RetailPaymentStatusType.cs.

    Note the collision: PartialPaid=3 matches, but Paid=4 (== SalePaymentStatusType.Refund).
    The repository maps these per sale type; never hardcode one enum for both.
    """
    New = 0
    Confirmed = 1
    ChangedToInvoice = 2
    PartialPaid = 3
    Paid = 4


class Rating(StrEnum):
    A = "A"
    B = "B"
    C = "C"
    D = "D"


class DebtLoadSource(StrEnum):
    DEBT_TABLE = "debt_table"
    LIVE_PROXY = "live_proxy"


class CapType(StrEnum):
    UTILIZATION_HARD_40 = "utilization_hard_40"
    UTILIZATION_SOFT_60 = "utilization_soft_60"
    BLOCKED_HALF = "blocked_half"


class SubFactor(BaseModel):
    """One sub-factor: raw 0..1 value AND its weighted points contribution (explainability)."""
    value: float = Field(..., ge=0.0, le=1.0)
    points: float
    weight: float


class SubFactors(BaseModel):
    discipline: SubFactor
    debt_load: SubFactor
    activity: SubFactor
    tenure: SubFactor
    return_quality: SubFactor


class CurrencyExposure(BaseModel):
    currency_id: int
    turnover_eur: float
    exposure_eur: float


class Contribution(BaseModel):
    """One feature's signed points in the current-state scorecard (explainability)."""
    feature: str
    value: float | None = None
    points: float


class ForwardRiskBand(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    VERY_HIGH = "very_high"


class ForwardRisk(BaseModel):
    """6-month forward (early-warning) risk: band + PD from the forward scorecard."""
    band: ForwardRiskBand
    pd: float


class SolvencyScore(BaseModel):
    client_id: int
    applicable: bool = True
    score: int | None = Field(default=None, ge=0, le=100)
    rating: Rating | None = None
    pd: float | None = Field(default=None, description="current-state PD (0..1)")
    contributions: list[Contribution] | None = None
    forward_risk: ForwardRisk | None = None
    sub_factors: SubFactors | None = None
    caps_applied: list[CapType] = Field(default_factory=list)
    debt_load_source: DebtLoadSource | None = None
    raw_score: float | None = Field(
        default=None, description="weighted sum * 100 before caps/rounding"
    )
    currency_breakdown: list[CurrencyExposure] | None = None
    as_of_date: str | None = None
    window_months: int = 12
    model_version: str = "creditscore-v3"


class GaugeChart(BaseModel):
    value: float
    threshold_soft: float = 0.9
    threshold_hard: float = 1.0
    label: str = "limit_utilization"


class DonutSlice(BaseModel):
    label: str
    count: int


class AgingBar(BaseModel):
    bucket: str
    count: int
    amount_eur: float | None = None


class TurnoverExposurePoint(BaseModel):
    period: str
    turnover_eur: float
    exposure_eur: float


class ScorePoint(BaseModel):
    period: str
    score: int


class TrendPoint(BaseModel):
    period: str
    turnover_eur: float


class SolvencyCharts(BaseModel):
    client_id: int
    limit_utilization_gauge: GaugeChart
    payment_discipline_donut: list[DonutSlice]
    open_invoice_aging_bars: list[AgingBar]
    turnover_vs_exposure: list[TurnoverExposurePoint]
    score_sparkline: list[ScorePoint]
    turnover_trend: list[TrendPoint]
    aging_over_time_heatmap: str = Field(
        default="pending",
        description="pending until Debt sync settles (not live-buildable yet)",
    )
    as_of_date: str | None = None
    window_months: int = 12
    model_version: str = "creditscore100-v2"


class ScoreRequest(BaseModel):
    client_id: int | None = Field(default=None, description="dbo.ClientAgreement.ClientID")
    client_net_uid: str | None = Field(default=None, description="dbo.Client.NetUID alternative")
    as_of_date: str | None = None
    window_months: int = Field(default=12, ge=1, le=60)
    use_cache: bool = True


class BatchScoreRequest(BaseModel):
    client_ids: list[int] = Field(..., min_length=1, max_length=500)
    as_of_date: str | None = None
    window_months: int = Field(default=12, ge=1, le=60)
    use_cache: bool = True
