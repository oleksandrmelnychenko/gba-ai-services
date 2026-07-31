"""Domain models for the CreditScore-100 solvency engine.

Contract is aligned with the future gba-server (.NET) DTOs and the console charts.
"""
from __future__ import annotations

import uuid
from datetime import date
from enum import IntEnum, StrEnum
from typing import Self

from pydantic import BaseModel, Field, field_validator, model_validator

from app.domain.money import round_cent


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


class DataSufficiency(StrEnum):
    OK = "ok"
    INSUFFICIENT = "insufficient"


class ClientIdentityMismatchError(ValueError):
    """Both supplied client identities exist but resolve to different entities."""


class CapType(StrEnum):
    UTILIZATION_HARD_40 = "utilization_hard_40"
    UTILIZATION_SOFT_60 = "utilization_soft_60"
    BLOCKED_HALF = "blocked_half"
    CURRENT_SEV180_PD_FLOOR = "current_sev180_pd_floor"


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
    currency_id: int = Field(gt=0)
    turnover_eur: float = Field(ge=0.0, allow_inf_nan=False)
    exposure_eur: float = Field(ge=0.0, allow_inf_nan=False)

    @field_validator("turnover_eur", "exposure_eur", mode="before")
    @classmethod
    def _round_money(cls, value):
        return float(round_cent(value))


class Contribution(BaseModel):
    """One feature's signed points in the current-state scorecard (explainability)."""
    feature: str
    value: float | None = Field(default=None, allow_inf_nan=False)
    points: float = Field(allow_inf_nan=False)


class ForwardRiskBand(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    VERY_HIGH = "very_high"


class ForwardRiskStatus(StrEnum):
    AVAILABLE = "available"
    NOT_APPLICABLE = "not_applicable"
    MODEL_UNAVAILABLE = "model_unavailable"


class ForwardRisk(BaseModel):
    """6-month forward (early-warning) risk: band + PD from the forward scorecard."""
    band: ForwardRiskBand
    pd: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)


class Risk90dBand(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class Risk90dReason(StrEnum):
    NO_DEBT = "no_debt"
    CURRENT_DEBT = "current_debt"
    WILL_CROSS_90_DAYS = "will_cross_90_days"
    ALREADY_90_PLUS = "already_90_plus"


class Risk90d(BaseModel):
    """Operational 90-day control derived from exact open-debt aging buckets."""

    horizon_days: int = Field(default=90, ge=1)
    threshold_days: int = Field(default=90, ge=1)
    band: Risk90dBand
    exposure_eur: float = Field(ge=0.0, allow_inf_nan=False)
    reason_code: Risk90dReason

    @field_validator("exposure_eur", mode="before")
    @classmethod
    def _round_exposure(cls, value):
        return float(round_cent(value))

    @model_validator(mode="after")
    def _validate_operational_contract(self) -> Self:
        valid = (
            self.horizon_days == 90
            and self.threshold_days == 90
            and (
                (
                    self.band == Risk90dBand.LOW
                    and self.reason_code == Risk90dReason.NO_DEBT
                    and self.exposure_eur == 0.0
                )
                or (
                    self.band == Risk90dBand.MEDIUM
                    and self.reason_code == Risk90dReason.CURRENT_DEBT
                    and self.exposure_eur > 0.0
                )
                or (
                    self.band == Risk90dBand.HIGH
                    and self.reason_code == Risk90dReason.WILL_CROSS_90_DAYS
                    and self.exposure_eur >= 100.0
                )
                or (
                    self.band == Risk90dBand.CRITICAL
                    and self.reason_code == Risk90dReason.ALREADY_90_PLUS
                    and self.exposure_eur >= 100.0
                )
            )
        )
        if not valid:
            raise ValueError("risk_90d operational fields are inconsistent")
        return self


class SolvencyScore(BaseModel):
    client_id: int
    client_net_uid: str | None = Field(
        default=None,
        description="canonical requested dbo.Client.NetUID for proxy identity validation",
    )
    applicable: bool = True
    score: int | None = Field(default=None, ge=0, le=100)
    rating: Rating | None = None
    pd: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        allow_inf_nan=False,
        description="current-state PD (0..1)",
    )
    contributions: list[Contribution] | None = None
    risk_90d: Risk90d | None = None
    forward_risk: ForwardRisk | None = None
    forward_risk_status: ForwardRiskStatus = ForwardRiskStatus.MODEL_UNAVAILABLE
    forward_risk_reason: str | None = None
    sub_factors: SubFactors | None = None
    caps_applied: list[CapType] = Field(default_factory=list)
    debt_load_source: DebtLoadSource | None = None
    raw_score: float | None = Field(
        default=None, description="weighted sum * 100 before caps/rounding"
    )
    currency_breakdown: list[CurrencyExposure] | None = None
    data_sufficiency: DataSufficiency = DataSufficiency.OK
    data_sufficiency_reason: str | None = None
    source_history_start: str = "2025-01-01"
    effective_start: str | None = None
    history_complete: bool = True
    as_of_date: str | None = None
    window_months: int = 12
    model_version: str = "creditscore-v3"
    current_model_run_id: str | None = None


class GaugeChart(BaseModel):
    value: float = Field(ge=0.0, allow_inf_nan=False)
    threshold_soft: float = Field(default=0.9, ge=0.0, allow_inf_nan=False)
    threshold_hard: float = Field(default=1.0, ge=0.0, allow_inf_nan=False)
    label: str = "limit_utilization"


class DonutSlice(BaseModel):
    label: str
    count: int


class AgingBar(BaseModel):
    bucket: str
    count: int
    amount_eur: float | None = Field(default=None, ge=0.0, allow_inf_nan=False)

    @field_validator("amount_eur", mode="before")
    @classmethod
    def _round_money(cls, value):
        return None if value is None else float(round_cent(value))


class TurnoverExposurePoint(BaseModel):
    period: str
    turnover_eur: float = Field(ge=0.0, allow_inf_nan=False)
    exposure_eur: float = Field(ge=0.0, allow_inf_nan=False)

    @field_validator("turnover_eur", "exposure_eur", mode="before")
    @classmethod
    def _round_money(cls, value):
        return float(round_cent(value))


class ScorePoint(BaseModel):
    period: str
    score: int


class TrendPoint(BaseModel):
    period: str
    turnover_eur: float = Field(ge=0.0, allow_inf_nan=False)

    @field_validator("turnover_eur", mode="before")
    @classmethod
    def _round_money(cls, value):
        return float(round_cent(value))


class SolvencyCharts(BaseModel):
    client_id: int
    applicable: bool = True
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
    source_history_start: str = "2025-01-01"
    effective_start: str | None = None
    history_complete: bool = True
    as_of_date: str | None = None
    window_months: int = 12
    model_version: str = "creditscore100-v2"


class ScoreRequest(BaseModel):
    client_id: int | None = Field(
        default=None, gt=0, description="dbo.ClientAgreement.ClientID"
    )
    client_net_uid: str | None = Field(default=None, description="dbo.Client.NetUID alternative")
    as_of_date: date | None = None
    window_months: int = Field(default=12, ge=1, le=60)
    use_cache: bool = True

    @model_validator(mode="after")
    def _require_client_identity(self) -> Self:
        if self.client_id is None and self.client_net_uid is None:
            raise ValueError("client_id or client_net_uid required")
        return self

    @field_validator("client_net_uid")
    @classmethod
    def _validate_net_uid(cls, v: str | None) -> str | None:
        if v is None:
            return v
        try:
            uuid.UUID(v)
        except (ValueError, AttributeError, TypeError) as exc:
            raise ValueError("client_net_uid must be a valid GUID") from exc
        return v


class BatchScoreRequest(BaseModel):
    client_ids: list[int] = Field(..., min_length=1, max_length=500)
    as_of_date: date | None = None
    window_months: int = Field(default=12, ge=1, le=60)
    use_cache: bool = True

    @field_validator("client_ids")
    @classmethod
    def _validate_client_ids(cls, values: list[int]) -> list[int]:
        if any(value <= 0 for value in values):
            raise ValueError("client_ids must contain only positive IDs")
        if len(values) != len(set(values)):
            raise ValueError("client_ids must not contain duplicates")
        return values
