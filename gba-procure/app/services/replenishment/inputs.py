"""Run-scoped plan inputs read once, set-based, and shared by every producer plan."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from app.core.config import get_settings
from app.data import cost_repository as cost_repo
from app.data import feedback, masters
from app.data import supply_repository as repo
from app.services.classify import service as classify_svc
from app.services.forecasting import lead_time as lead_time_svc
from app.services.replenishment import derived_terms


@dataclass(frozen=True)
class PlanInputs:
    as_of: str
    product_scope: frozenset[int]
    producer_scope: frozenset[int]
    on_hand: dict[int, float]
    reserved: dict[int, float]
    on_order: dict[int, float]
    demand_rows: dict[int, list[dict]]
    sales: dict[int, float]
    abc_map: dict[int, str]
    lead_time_samples: dict[int, list[float]]
    currencies: dict[int, int]
    names: dict[int, str | None]
    profiles: dict[int, dict] | None
    terms: dict[int, dict[int, dict]]
    factors: dict[int, dict[str, float]]
    measure_units: dict[int, str]
    fallback_costs: dict[tuple[int, int], float]
    lead_time_sources: dict[str, int] = field(default_factory=dict)
    derived_terms: dict[int, dict[int, dict]] = field(default_factory=dict)

    def covers(self, producer_id: int, product_ids: list[int], as_of: str) -> bool:
        return (
            as_of == self.as_of
            and producer_id in self.producer_scope
            and self.product_scope.issuperset(product_ids)
        )

    def lead_time(self, producer_id: int) -> tuple[float, float, str]:
        return lead_time_svc.lead_time_from(
            self.lead_time_samples.get(producer_id, []), self.currencies.get(producer_id)
        )


def load_plan_inputs(
    as_of: str, producer_ids: list[int], products_by_producer: dict[int, list[int]]
) -> PlanInputs:
    s = get_settings()
    producers = sorted({int(p) for p in producer_ids})
    products = sorted({int(p) for ids in products_by_producer.values() for p in ids})
    on_hand, reserved = repo.inventory_bulk(products)
    wanted = set(products)
    samples = repo.producer_lead_times_all(as_of, s.lead_time_min_days, s.lead_time_max_days)
    measure_units = repo.product_measure_units(products)
    currencies = repo.producer_agreement_currencies_all(as_of)
    inputs = PlanInputs(
        as_of=as_of,
        product_scope=frozenset(products),
        producer_scope=frozenset(producers),
        on_hand=on_hand,
        reserved=reserved,
        on_order={pid: qty for pid, qty in repo.on_order_all(as_of).items() if pid in wanted},
        demand_rows=repo.product_daily_demand_bulk(products, as_of, s.history_days),
        sales=cost_repo.sale_prices_eur(products, as_of, s.history_days),
        abc_map=classify_svc.get_abc_map(as_of, s.history_days),
        lead_time_samples=samples,
        currencies=currencies,
        names=repo.producer_names_raw(producers),
        profiles=masters.producer_profiles_all(required=True) if s.use_masters else None,
        terms=masters.product_terms_all(required=True) if s.use_masters else {},
        factors=(
            feedback.learned_factors_all(
                s.feedback_min_samples, s.override_factor_min, s.override_factor_max
            )
            if s.use_feedback
            else {}
        ),
        measure_units=measure_units,
        fallback_costs=repo.last_document_costs_eur(as_of),
        derived_terms=derived_terms.validated_terms(
            as_of,
            {int(p): products_by_producer.get(p, []) for p in producers},
            measure_units,
        ),
    )
    sources: dict[str, int] = {}
    for producer in producers:
        mean, _std, source = inputs.lead_time(producer)
        if math.isfinite(mean):
            sources[source] = sources.get(source, 0) + 1
    inputs.lead_time_sources.update(sources)
    return inputs
