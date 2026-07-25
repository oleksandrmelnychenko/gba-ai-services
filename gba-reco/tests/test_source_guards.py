"""Always-on source guards — no DB/Redis, run in normal pytest/CI.

These read the repository/recommender source via inspect.getsource and assert the
correctness-fix SQL patterns are present/absent. Mocked unit tests stayed green while
real bugs shipped (only live smoke caught them); these guards make reintroducing a fix
regression fail CI immediately, with no database required.

Fixes guarded:
- the validity filter migrated from the wrong/absent `o.Deleted = 0` order-level predicate
  to the correct item-level `oi.IsValidForCurrentSale = 1` (the actual sales-spine validity
  column) across every query that filters the rec population;
- the synthetic accounting line (the debt-entry product «Ввід боргів») is excluded explicitly —
  resolved by Name at runtime because catalog re-syncs re-mint its ProductID
  (25422404 → 29555414 → ...) — not left to drift with the data-driven ubiquity threshold.
"""
from __future__ import annotations

import inspect
import re

from app.core import config
from app.data import sales_repository
from app.services.eval import baselines, harness
from app.services.recommendations import als, copurchase, worker

VALIDITY_MODULES = {
    "copurchase": copurchase,
    "als": als,
    "baselines": baselines,
    "worker": worker,
    "harness": harness,
    "sales_repository": sales_repository,
}

_DELETED_PATTERN = re.compile(r"o\.Deleted\s*=\s*0")
_VALIDITY_PATTERN = re.compile(r"oi\.IsValidForCurrentSale\s*=\s*1")


def test_no_order_deleted_predicate_anywhere():
    for name, module in VALIDITY_MODULES.items():
        src = inspect.getsource(module)
        offenders = _DELETED_PATTERN.findall(src)
        assert not offenders, (
            f"{name}: order-level `o.Deleted = 0` validity predicate reintroduced "
            f"(must use item-level oi.IsValidForCurrentSale = 1): {offenders}"
        )


def test_is_valid_for_current_sale_present_in_each_validity_query():
    for name in ("copurchase", "als", "baselines", "worker", "harness"):
        src = inspect.getsource(VALIDITY_MODULES[name])
        assert _VALIDITY_PATTERN.search(src), (
            f"{name}: lost the `oi.IsValidForCurrentSale = 1` validity filter"
        )


def test_ubiquity_query_uses_valid_population_not_deleted_flag():
    src = inspect.getsource(sales_repository._query_ubiquitous)
    assert _VALIDITY_PATTERN.search(src), (
        "ubiquity query must filter on oi.IsValidForCurrentSale = 1 (same valid "
        "population the recommender uses), not an order-level deleted flag"
    )
    assert not _DELETED_PATTERN.search(src), "ubiquity query reintroduced o.Deleted = 0"


def test_every_live_recommendation_history_query_uses_item_validity():
    functions = (
        sales_repository.count_orders_before,
        sales_repository.repurchase_rate,
        sales_repository.product_frequency,
        sales_repository.product_last_purchase,
        sales_repository.customer_products,
        sales_repository.candidate_similar_customers,
        sales_repository.customer_products_bulk,
        sales_repository.collaborative_products,
    )
    for function in functions:
        src = inspect.getsource(function)
        assert _VALIDITY_PATTERN.search(src), (
            f"{function.__name__}: recommendation history query lost "
            "oi.IsValidForCurrentSale = 1"
        )


def test_stock_filter_is_scoped_to_operational_resale_storages():
    src = inspect.getsource(sales_repository.in_stock_product_ids)
    assert "JOIN dbo.Storage" in src
    assert "s.Deleted = 0" in src
    assert "s.AvailableForReSale = 1 OR s.IsResale = 1" in src
    assert "CASE WHEN d.Deleted = 0 THEN d.ID ELSE MAX(l.ID) END" in src


def test_synthetic_product_exclusion_resolves_live_row_by_name():
    """Catalog re-syncs re-mint the debt-entry row under a NEW ProductID (25422404 → 29555414 →
    ...), so a hardcoded pin goes stale. The live id must be resolved by Name at runtime; the
    Settings field is an explicit env override only (default EMPTY, never a stale constant)."""
    module_src = inspect.getsource(sales_repository)
    fn_src = inspect.getsource(sales_repository.synthetic_product_ids)
    assert "Ввід боргів" in module_src, (
        "sales_repository lost the debt-entry Name key («Ввід боргів») used for dynamic resolution"
    )
    assert re.search(r"Deleted\s*=\s*0", fn_src) and "ORDER BY ID DESC" in fn_src, (
        "synthetic_product_ids must resolve the LIVE debt-entry row "
        "(WHERE Name = ... AND Deleted = 0 ORDER BY ID DESC)"
    )
    field = config.Settings.model_fields["synthetic_product_ids"]
    assert field.default == frozenset(), (
        "Settings.synthetic_product_ids default must stay EMPTY — a hardcoded id goes stale on "
        "the next catalog re-mint; the env var is an explicit override only"
    )


def test_ubiquity_helper_references_synthetic_ids_resolver():
    src = inspect.getsource(sales_repository.ubiquitous_product_ids)
    assert "synthetic_product_ids" in src, (
        "ubiquitous_product_ids must UNION the explicit synthetic_product_ids resolver so "
        "exclusion never depends on the data-driven ubiquity threshold catching the debt line"
    )


def test_synthetic_exclusion_is_unconditional_in_ubiquity():
    src = inspect.getsource(sales_repository.ubiquitous_product_ids)
    assert re.search(r"synthetic_product_ids\(\)\s*\|", src), (
        "synthetic ids must be UNION'd unconditionally (synthetic_product_ids() | <ubiquity set>), "
        "so the pinned exclusion is independent of the rolling ubiquity window"
    )


def test_precision_estimate_is_not_the_fabricated_value():
    """The contract's precision_estimate must NOT be the old hardcoded 0.754 (contradicted by the
    harness by ~23x). It must be a harness-derived figure aligned with the committed baseline."""
    from app.domain.models import RecommendationResult
    from app.services.eval.harness import BASELINE_V32

    default = RecommendationResult.model_fields["precision_estimate"].default
    assert abs(default - 0.754) > 1e-6, "fabricated precision_estimate 0.754 reintroduced"
    assert abs(default - BASELINE_V32["precision"]) < 1e-6, (
        "precision_estimate must equal the harness-derived baseline precision@10 "
        f"({BASELINE_V32['precision']}); update both together when the model is re-measured"
    )


def test_region_scoping_uses_region_id_natural_key_not_per_client_code():
    """byRegion scoping must group on dbo.Client.RegionID (the oblast, ~26 groups), NOT
    RegionCodeID (per-client address granularity that does not group)."""
    src = inspect.getsource(sales_repository.candidate_similar_customers)
    assert "RegionID" in src, "region scoping lost the Client.RegionID grouping key"
    assert "RegionCodeID" not in src, (
        "region scoping must NOT use the per-client RegionCodeID (it does not group clients)"
    )
