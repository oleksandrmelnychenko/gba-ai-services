"""Domain models for procurement / replenishment."""
from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


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
    on_hand: float = 0.0      # ProductAvailability.Amount
    reserved: float = 0.0     # ProductReservation
    on_order: float = 0.0     # open SupplyOrder not yet arrived
    available: float = 0.0    # on_hand - reserved
    position: float = 0.0     # available + on_order


class CheaperAlt(BaseModel):
    producer_id: int
    cost_eur: float


class ReorderSuggestion(BaseModel):
    product_id: int
    producer_id: int
    suggested_qty: float
    reorder_point: float
    safety_stock: float
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


class ProducerPurchasePlan(BaseModel):
    producer_id: int
    producer_name: str | None = None
    lead_time_days: float
    lead_time_std_days: float = 0.0
    lead_time_source: str = "default"
    items: list[ReorderSuggestion]
    item_count: int
    as_of_date: str | None = None
    model_version: str = "procure-hist120-v1"


class CartPlanRequest(BaseModel):
    as_of_date: str | None = None
    only_needed: bool = True
    limit: int | None = 200
    budget_eur: float | None = None


class CartReplenishmentPlan(BaseModel):
    items: list[ReorderSuggestion]
    item_count: int
    as_of_date: str | None = None
    budget_eur: float | None = None
    budget_used_eur: float | None = None
    value_captured_eur: float | None = None
    selected_count: int | None = None
    deferred_count: int | None = None
    model_version: str = "procure-hist120-v1"


# --- dashboard chart data (derived from build_plan; no policy/forecast math change) ---

class UrgencyMixBucket(BaseModel):
    urgency: Urgency
    count: int


class DaysOfCoverBucket(BaseModel):
    bucket: str = Field(description="one of '<0' | '0-7' | '8-30' | '31-90' | '90+'")
    count: int


class TopItem(BaseModel):
    product_id: int
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
    points: list[DemandPoint]


class PlanCharts(BaseModel):
    producer_id: int | None = None
    as_of_date: str | None = None
    top_n: int = 15
    urgency_mix: list[UrgencyMixBucket]
    days_of_cover_hist: list[DaysOfCoverBucket]
    top_items: list[TopItem]
    demand_series: list[DemandSeries]
    model_version: str = "procure-hist120-v1"
