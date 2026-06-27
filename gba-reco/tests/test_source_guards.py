"""Always-on source guards — no DB/Redis, run in normal pytest/CI.

These read the repository/recommender source via inspect.getsource and assert the
correctness-fix SQL patterns are present/absent. Mocked unit tests stayed green while
real bugs shipped (only live smoke caught them); these guards make reintroducing a fix
regression fail CI immediately, with no database required.

Fixes guarded:
- the validity filter migrated from the wrong/absent `o.Deleted = 0` order-level predicate
  to the correct item-level `oi.IsValidForCurrentSale = 1` (the actual sales-spine validity
  column) across every query that filters the rec population;
- the synthetic accounting line (debt-entry ProductID 25422404) is excluded explicitly via a
  config constant, not left to drift with the data-driven ubiquity threshold.
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


def test_synthetic_product_exclusion_is_explicit_constant():
    assert 25422404 in config.get_settings().synthetic_product_ids, (
        "synthetic debt-entry line 25422404 must be pinned in Settings.synthetic_product_ids"
    )
    field = config.Settings.model_fields["synthetic_product_ids"]
    assert 25422404 in field.default, (
        "25422404 must be the *default* synthetic exclusion (pinned in source, "
        "not only an env override)"
    )


def test_ubiquity_helper_references_synthetic_ids_constant():
    src = inspect.getsource(sales_repository.ubiquitous_product_ids)
    assert "synthetic_product_ids" in src, (
        "ubiquitous_product_ids must UNION the explicit synthetic_product_ids constant so "
        "exclusion never depends on the data-driven ubiquity threshold catching 25422404"
    )


def test_synthetic_exclusion_is_unconditional_in_ubiquity():
    src = inspect.getsource(sales_repository.ubiquitous_product_ids)
    assert re.search(r"synthetic_product_ids\s*\|", src), (
        "synthetic ids must be UNION'd unconditionally (s.synthetic_product_ids | <ubiquity set>), "
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
