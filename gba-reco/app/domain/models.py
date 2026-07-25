"""Domain models for client recommendations."""
from __future__ import annotations

import math
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.history import parse_as_of, source_history_start


class Segment(StrEnum):
    HEAVY = "HEAVY"
    REGULAR_CONSISTENT = "REGULAR_CONSISTENT"
    REGULAR_EXPLORATORY = "REGULAR_EXPLORATORY"
    LIGHT = "LIGHT"


class RecSource(StrEnum):
    REPURCHASE = "repurchase"
    DISCOVERY = "discovery"


class RecSourceDetail(StrEnum):
    """Concrete evidence path behind the backwards-compatible broad source."""

    REPURCHASE_HISTORY = "repurchase_history"
    SIMILAR_CLIENTS = "similar_clients"
    COPURCHASE = "copurchase"
    GLOBAL_POPULAR = "global_popular"


class ProductRec(BaseModel):
    """One recommended product (contract aligned with gba-server .NET DTO)."""

    model_config = ConfigDict(allow_inf_nan=False)

    product_id: int = Field(gt=0)
    score: float = Field(ge=0)
    rank: int = Field(gt=0)
    segment: str = Field(min_length=1)
    source: RecSource
    source_detail: RecSourceDetail

    @model_validator(mode="after")
    def validate_source_detail(self) -> Self:
        is_history = self.source_detail == RecSourceDetail.REPURCHASE_HISTORY
        if (self.source == RecSource.REPURCHASE) != is_history:
            raise ValueError(
                "repurchase source must use repurchase_history and discovery must use "
                "a discovery source_detail"
            )
        return self


class RecommendationResult(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)

    customer_id: int = Field(gt=0)
    recommendations: list[ProductRec]
    count: int = Field(ge=0)
    discovery_count: int = Field(ge=0)
    segment: str = Field(min_length=1)
    precision_estimate: float = Field(
        default=0.033,
        ge=0,
        le=1,
        description=(
            "Harness-derived precision@10 for the v3.2 model on the leave-last-basket eval "
            "(n=409, synthetic/ubiquitous excluded; see docs/eval-baseline.md). NOT a per-call "
            "confidence — it is the model's measured offline precision. Was a fabricated 0.754; "
            "replaced with the real measured number so the contract carries an honest metric "
            "(the .NET DTO field is non-nullable double, so the value is kept rather than omitted)."
        ),
    )
    latency_ms: float = Field(default=0.0, ge=0)
    cached: bool = False
    as_of_date: str | None = None
    source_history_start: str
    effective_start: str
    history_complete: bool
    model_version: str = "v38-history-floor-20250101-source-detail-202607"

    @model_validator(mode="after")
    def validate_exact_contract(self) -> Self:
        """Make stale/corrupt cache entries fail closed instead of leaking partial output."""
        if not math.isfinite(self.precision_estimate) or not math.isfinite(self.latency_ms):
            raise ValueError("recommendation metrics must be finite")
        if self.count != len(self.recommendations):
            raise ValueError("count must equal recommendations length")
        product_ids = [item.product_id for item in self.recommendations]
        if len(product_ids) != len(set(product_ids)):
            raise ValueError("recommendation product ids must be unique")
        expected_ranks = list(range(1, len(self.recommendations) + 1))
        if [item.rank for item in self.recommendations] != expected_ranks:
            raise ValueError("recommendation ranks must be contiguous and one-based")
        if any(item.segment != self.segment for item in self.recommendations):
            raise ValueError("every recommendation segment must equal the response segment")
        if any(
            (item.source == RecSource.REPURCHASE)
            != (item.source_detail == RecSourceDetail.REPURCHASE_HISTORY)
            for item in self.recommendations
        ):
            raise ValueError("recommendation source and source_detail must stay consistent")
        actual_discovery = sum(
            item.source == RecSource.DISCOVERY for item in self.recommendations
        )
        if self.discovery_count != actual_discovery:
            raise ValueError("discovery_count must match recommendation sources")
        history_start = parse_as_of(self.source_history_start)
        effective_start = parse_as_of(self.effective_start)
        if history_start != source_history_start():
            raise ValueError("source_history_start must match the configured source boundary")
        if effective_start < history_start:
            raise ValueError("effective_start cannot precede source_history_start")
        if self.as_of_date is not None and parse_as_of(self.as_of_date) < effective_start:
            raise ValueError("as_of_date cannot precede effective_start")
        return self
