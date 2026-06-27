"""Domain models for client recommendations."""
from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class Segment(StrEnum):
    HEAVY = "HEAVY"
    REGULAR_CONSISTENT = "REGULAR_CONSISTENT"
    REGULAR_EXPLORATORY = "REGULAR_EXPLORATORY"
    LIGHT = "LIGHT"


class RecSource(StrEnum):
    REPURCHASE = "repurchase"
    DISCOVERY = "discovery"


class ProductRec(BaseModel):
    """One recommended product (contract aligned with gba-server .NET DTO)."""
    product_id: int
    score: float
    rank: int
    segment: str
    source: RecSource


class RecommendationResult(BaseModel):
    customer_id: int
    recommendations: list[ProductRec]
    count: int
    discovery_count: int
    segment: str
    precision_estimate: float = Field(
        default=0.033,
        description=(
            "Harness-derived precision@10 for the v3.2 model on the leave-last-basket eval "
            "(n=409, synthetic/ubiquitous excluded; see docs/eval-baseline.md). NOT a per-call "
            "confidence — it is the model's measured offline precision. Was a fabricated 0.754; "
            "replaced with the real measured number so the contract carries an honest metric "
            "(the .NET DTO field is non-nullable double, so the value is kept rather than omitted)."
        ),
    )
    latency_ms: float = 0.0
    cached: bool = False
    as_of_date: str | None = None
    model_version: str = "v33-realdata-202606"
