"""Domain models for procurement / replenishment."""
from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, model_validator

from app.core.config import get_settings
from app.core.history import rolling_coverage

_MODEL_SETTINGS = get_settings()
MODEL_VERSION = (
    f"procure-hist{_MODEL_SETTINGS.history_days}-"
    f"floor{_MODEL_SETTINGS.source_history_start_date:%Y%m%d}-v2"
)


class HistoryContractModel(BaseModel):
    as_of_date: str | None = None
    source_history_start: str = Field(
        default_factory=lambda: get_settings().source_history_start_date.isoformat()
    )
    effective_start: str | None = None
    effective_history_days: int = 0
    history_complete: bool = False
    history_not_applicable: list[str] = Field(
        default_factory=lambda: ["inventory", "reservations"]
    )
    model_version: str = MODEL_VERSION

    @model_validator(mode="before")
    @classmethod
    def populate_history_contract(cls, value: Any) -> Any:
        if not isinstance(value, dict) or not value.get("as_of_date"):
            return value
        result = dict(value)
        coverage = rolling_coverage(
            result["as_of_date"],
            get_settings().history_days,
        )
        for key, item in coverage.as_metadata().items():
            result.setdefault(key, item)
        result.setdefault("history_not_applicable", ["inventory", "reservations"])
        result.setdefault("model_version", MODEL_VERSION)
        return result


class Urgency(StrEnum):
    CRITICAL = "critical"   # already below safety stock / stocked out
    HIGH = "high"           # will breach reorder point within lead time
    NORMAL = "normal"       # replenish on schedule
    NONE = "none"           # sufficient cover


class DemandForecast(BaseModel):
    product_id: int
    mean_daily: float = Field(description="Forecast mean demand per day (units)")
    std_daily: float = Field(default=0.0, description="Demand std/day for safety stock")
    method: str = Field(default="naive", description="forecasting method id")
    horizon_days: int = 30
    forecast_units: float = Field(description="Expected demand over horizon")


class InventoryPosition(BaseModel):
    product_id: int
    on_hand: float = 0.0      # gross physical = ProductAvailability.Amount + active reservations
    reserved: float = 0.0     # ProductReservation
    on_order: float = 0.0     # packed/committed supply not yet received
    available: float = 0.0    # on_hand - reserved
    position: float = 0.0     # available + on_order


class CheaperAlt(BaseModel):
    producer_id: int
    cost_eur: float


class ReorderSuggestion(BaseModel):
    product_id: int
    product_name: str | None = None
    vendor_code: str | None = None
    oe_number: str | None = None
    image_url: str | None = None
    producer_id: int
    producer_name: str | None = None
    suggested_qty: float
    reorder_point: float
    safety_stock: float
    # Explicit proof components so the console can render the full breakdown:
    #   lead_demand + safety_stock = reorder_point ; order_up_to − position = suggested_qty.
    lead_demand: float | None = None
    order_up_to: float | None = None
    days_of_cover: float = Field(description="Days current position lasts at forecast demand")
    urgency: Urgency
    forecast: DemandForecast
    inventory: InventoryPosition
    reason: str
    unit_cost_eur: float | None = None
    line_cost_eur: float | None = None
    unit_sale_eur: float | None = None
    unit_margin_eur: float | None = None
    applied_service_level: float | None = None
    abc: str | None = None
    xyz: str | None = None
    quadrant: str | None = None
    seasonal_factor: float | None = None
    raw_qty: float | None = None
    moq: float | None = None
    order_multiple: float | None = None
    learned_factor: float | None = None
    value_density: float | None = None
    within_budget: bool | None = None
    cheaper_alt: CheaperAlt | None = None


class ProducerPurchasePlan(HistoryContractModel):
    producer_id: int
    producer_name: str | None = None
    lead_time_days: float
    lead_time_std_days: float = 0.0
    lead_time_source: str = "default"
    items: list[ReorderSuggestion]
    item_count: int


class CartPlanRequest(BaseModel):
    as_of_date: str | None = None
    only_needed: bool = True
    limit: int | None = 200
    budget_eur: float | None = None


class CartReplenishmentPlan(HistoryContractModel):
    items: list[ReorderSuggestion]
    item_count: int
    total_item_count: int = 0
    is_truncated: bool = False
    duplicate_supplier_options_removed: int = 0
    total_suggested_qty: float = 0.0
    total_cost_eur: float | None = 0.0
    priced_cost_eur: float = 0.0
    unpriced_item_count: int = 0
    budget_eur: float | None = None
    budget_used_eur: float | None = None
    value_captured_eur: float | None = None
    selected_count: int | None = None
    deferred_count: int | None = None
    method_used: str | None = None


# --- dashboard chart data (derived from build_plan; no policy/forecast math change) ---

class UrgencyMixBucket(BaseModel):
    urgency: Urgency
    count: int


class DaysOfCoverBucket(BaseModel):
    bucket: str = Field(description="one of '<0' | '0-7' | '8-30' | '31-90' | '90+'")
    count: int


class TopItem(BaseModel):
    product_id: int
    product_name: str | None = None
    vendor_code: str | None = None
    oe_number: str | None = None
    image_url: str | None = None
    producer_id: int | None = None
    producer_name: str | None = None
    suggested_qty: float
    on_hand: float
    reorder_point: float
    urgency: Urgency


class DemandPoint(BaseModel):
    period: str = Field(description="month 'yyyy-MM'")
    units: float
    is_forecast: bool = False


class DemandSeries(BaseModel):
    product_id: int
    product_name: str | None = None
    vendor_code: str | None = None
    oe_number: str | None = None
    image_url: str | None = None
    producer_id: int | None = None
    producer_name: str | None = None
    points: list[DemandPoint]


class PlanCharts(HistoryContractModel):
    producer_id: int | None = None
    top_n: int = 15
    urgency_mix: list[UrgencyMixBucket]
    days_of_cover_hist: list[DaysOfCoverBucket]
    top_items: list[TopItem]
    demand_series: list[DemandSeries]
