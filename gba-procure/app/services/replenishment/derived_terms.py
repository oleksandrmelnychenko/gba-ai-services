"""Minimum-order terms derived from supply documents and enabled only when they hold up.

A term is the smallest quantity of a product on one supply document of a producer, given at
least `derived_moq_min_documents` documents since the source history start. It is applied to
a plan line only when no sanity check objects; every other derived term stays off until a
buyer reviews it. Nothing here is stored: the terms are recomputed from the documents on
every run.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date

from app.core.config import get_settings
from app.core.history import as_date
from app.core.logging import get_logger
from app.data import supply_repository as repo

log = get_logger("derived_terms")

BASIS_DERIVED_VALIDATED = "derived_validated"
BASIS_BUYER = "buyer"

FLAG_SOURCE_OLD = "source_document_older_than_18_months"
FLAG_ABOVE_YEAR_SALES = "minimum_exceeds_12_month_sales"
FLAG_NO_SALES = "no_sales_in_12_months"
FLAG_NOT_MULTIPLES_OF_MINIMUM = "other_documents_not_multiples_of_minimum"
FLAG_MINIMUM_NOT_PACK_MULTIPLE = "minimum_not_multiple_of_pack"
FLAG_DELIVERIES_NOT_PACK_MULTIPLES = "deliveries_not_multiples_of_pack"

_TOLERANCE = 1e-6


@dataclass(frozen=True)
class DerivedTerm:
    producer_id: int
    product_id: int
    moq: float
    pack: float | None
    effective_moq: float
    documents: int
    quantities: tuple[float, ...]
    source_document_date: str
    sales_12m_units: float
    flags: tuple[str, ...]

    @property
    def validated(self) -> bool:
        return not self.flags

    def as_plan_term(self) -> dict:
        return {
            "moq": self.moq,
            "order_multiple": self.pack,
            "moq_basis": BASIS_DERIVED_VALIDATED,
            "documents": self.documents,
            "quantities": list(self.quantities),
            "source_document_date": self.source_document_date,
            "sales_12m_units": self.sales_12m_units,
        }


def _is_multiple(quantity: float, base: float) -> bool:
    ratio = quantity / base
    return abs(ratio - round(ratio)) < _TOLERANCE


def _months_before(day: date, months: int) -> date:
    index = day.year * 12 + day.month - 1 - months
    year, month = divmod(index, 12)
    month += 1
    leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
    last = (31, 29 if leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)[month - 1]
    return date(year, month, min(day.day, last))


def effective_minimum(moq: float, pack: float | None, unit: str | None) -> float:
    from app.services.replenishment.policy import _whole_units

    quantity = moq
    if pack:
        quantity = math.ceil(round(quantity / pack, 9)) * pack
    return _whole_units(quantity, unit)


def evaluate(
    producer_id: int,
    product_id: int,
    documents: list[tuple[str, int, float]],
    pack_standard: float | None,
    sales_12m_units: float,
    unit: str | None,
    as_of: str | date,
) -> DerivedTerm:
    """`documents` are (document date, document id, quantity) of one producer and product."""
    s = get_settings()
    quantities = sorted(float(q) for _d, _i, q in documents)
    moq = quantities[0]
    pack = float(pack_standard) if pack_standard and pack_standard > 1 else None
    source_date, _source_id = max((d, i) for d, i, q in documents if float(q) == moq)
    effective = effective_minimum(moq, pack, unit)
    flags: list[str] = []
    if as_date(source_date) < _months_before(as_date(as_of), s.derived_moq_source_max_age_months):
        flags.append(FLAG_SOURCE_OLD)
    if effective > sales_12m_units:
        flags.append(FLAG_ABOVE_YEAR_SALES if sales_12m_units > 0 else FLAG_NO_SALES)
    if moq > 1:
        others = quantities[1:]
        if sum(1 for q in others if _is_multiple(q, moq)) * 2 < len(others):
            flags.append(FLAG_NOT_MULTIPLES_OF_MINIMUM)
    if pack:
        if not _is_multiple(moq, pack):
            flags.append(FLAG_MINIMUM_NOT_PACK_MULTIPLE)
        if sum(1 for q in quantities if _is_multiple(q, pack)) * 2 < len(quantities):
            flags.append(FLAG_DELIVERIES_NOT_PACK_MULTIPLES)
    return DerivedTerm(
        producer_id=int(producer_id),
        product_id=int(product_id),
        moq=moq,
        pack=pack,
        effective_moq=effective,
        documents=len(quantities),
        quantities=tuple(quantities),
        source_document_date=str(source_date)[:10],
        sales_12m_units=float(sales_12m_units),
        flags=tuple(flags),
    )


def derive(
    as_of: str,
    products_by_producer: dict[int, list[int]],
    measure_units: dict[int, str] | None = None,
) -> list[DerivedTerm]:
    """Every derivable term of the given producer/product pairs, validated or not."""
    s = get_settings()
    wanted = {(int(pr), int(p)) for pr, ids in products_by_producer.items() for p in ids}
    if not wanted:
        return []
    only = next(iter(products_by_producer)) if len(products_by_producer) == 1 else None
    evidence = {
        pair: found
        for pair, found in repo.supply_document_quantities(
            as_of, s.derived_moq_min_documents, producer_id=only
        ).items()
        if pair in wanted
    }
    products = sorted({product for _producer, product in evidence})
    sold = repo.net_units_sold(products, as_of, s.derived_moq_sales_window_days)
    units = measure_units if measure_units is not None else repo.product_measure_units(products)
    return [
        evaluate(
            producer,
            product,
            found["documents"],
            found["pack"],
            sold.get(product, 0.0),
            units.get(product),
            as_of,
        )
        for (producer, product), found in sorted(evidence.items())
    ]


def summary(terms: list[DerivedTerm]) -> dict:
    flags: dict[str, int] = {}
    for term in terms:
        for flag in term.flags:
            flags[flag] = flags.get(flag, 0) + 1
    validated = [term for term in terms if term.validated]
    return {
        "derived": len(terms),
        "validated": len(validated),
        "validated_with_pack_multiple": sum(1 for term in validated if term.pack),
        "held_for_review": len(terms) - len(validated),
        "flags": dict(sorted(flags.items())),
    }


def validated_terms(
    as_of: str,
    products_by_producer: dict[int, list[int]],
    measure_units: dict[int, str] | None = None,
) -> dict[int, dict[int, dict]]:
    """{producer: {product: plan term}} for the terms no sanity check objects to."""
    if not get_settings().derived_moq_enabled:
        return {}
    terms = derive(as_of, products_by_producer, measure_units)
    if len(products_by_producer) > 1:
        log.info("derived_terms", as_of=as_of, **summary(terms))
    out: dict[int, dict[int, dict]] = {}
    for term in terms:
        if term.validated:
            out.setdefault(term.producer_id, {})[term.product_id] = term.as_plan_term()
    return out
