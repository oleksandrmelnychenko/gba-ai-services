"""Always-on source guards: no DB/Redis, run in normal pytest/CI.

They read the service source via inspect.getsource and assert that every purchase-history
query is built from app.data.source_rules (gba-server's confirmed sale net of buyer returns),
that the synthetic debt-entry line is resolved by name, and that the stock filter and the
model namespace keep their contracts.
"""
from __future__ import annotations

import inspect
import re
from datetime import date, datetime
from pathlib import Path

from app.core import config
from app.data import sales_repository
from app.data import source_rules as rules
from app.services.eval import baselines, harness, holdout
from app.services.recommendations import als, copurchase, snapshot, worker

_APP = Path(sales_repository.__file__).resolve().parents[1]
_PRIVATE_DEFINITION = re.compile(
    r"o\.Created|IsValidForCurrentSale|ChangedToInvoice|dbo\.OrderItem|dbo\.\[Order\]|dbo\.Sale\b"
)

HISTORY_QUERIES = (
    sales_repository.source_readiness,
    sales_repository._query_ubiquitous,
    sales_repository.count_orders_before,
    sales_repository.repurchase_rate,
    sales_repository.product_frequency,
    sales_repository.product_last_purchase,
    sales_repository.owned_live_product_ids,
    sales_repository.customer_products,
    sales_repository.candidate_similar_customers,
    sales_repository.customer_products_bulk,
    sales_repository.collaborative_products,
    copurchase._client_products_with_freq,
    copurchase._cooccurring_products,
    copurchase._product_degrees,
    als._load_interactions,
    baselines.most_frequent_for_client,
    baselines.global_popular,
    harness.build_cases,
    holdout._truth,
    worker.active_clients,
    snapshot.load_data,
)


def test_purchase_definition_lives_only_in_source_rules():
    offenders = {}
    for path in sorted(_APP.rglob("*.py")):
        if path.name == "source_rules.py":
            continue
        found = _PRIVATE_DEFINITION.findall(path.read_text(encoding="utf-8"))
        if found:
            offenders[str(path.relative_to(_APP))] = sorted(set(found))
    assert not offenders, f"purchase history defined outside app.data.source_rules: {offenders}"


def test_every_history_query_uses_the_shared_rule_and_window():
    for function in HISTORY_QUERIES:
        src = inspect.getsource(function)
        name = f"{function.__module__}.{function.__name__}"
        assert "{rules.SPINE}" in src, f"{name}: not built on source_rules.SPINE"
        assert "{rules.PURCHASED}" in src, f"{name}: not filtered by source_rules.PURCHASED"
        assert re.search(r"rules\.(window|trailing_window|forward_window)\(", src), (
            f"{name}: window is not bound through source_rules"
        )


def test_multi_population_queries_apply_the_rule_to_every_branch():
    assert inspect.getsource(sales_repository.collaborative_products).count("{rules.PURCHASED}") == 2
    assert inspect.getsource(copurchase._cooccurring_products).count("{rules.PURCHASED}") == 2


def test_rule_is_the_gba_server_confirmed_sale_net_of_returns():
    sql = rules.PURCHASED
    for fragment in (
        "s.Deleted = 0",
        "s.ChangedToInvoice IS NOT NULL",
        "NOT EXISTS (SELECT 1 FROM dbo.DataSyncAccountingSourceTarget carrier",
        "carrier.TargetEntityType = N'Sale'",
        "carrier.IsCurrent = 1 AND carrier.OwnershipType = 1",
        "oi.Deleted = 0",
        "oi.IsValidForCurrentSale = 1",
        "oi.Qty > 0",
        "oi.ProductID IS NOT NULL",
        "s.ChangedToInvoice >= :sold_from",
        "s.ChangedToInvoice < :sold_to",
        "dbo.SaleReturnItem sri",
        "sri.Deleted = 0",
        "sr.Deleted = 0 AND sr.IsCanceled = 0",
        "sr.FromDate < :sold_to",
        "HAVING SUM(CAST(sri.Qty AS decimal(38, 6))) >= CAST(roi.Qty AS decimal(38, 6))",
        "returned.line_id = oi.ID",
    ):
        assert fragment in sql, f"confirmed-sale rule lost `{fragment}`"
    assert "NOT EXISTS (SELECT 1 FROM (SELECT sri.OrderItemID AS line_id" in sql
    assert re.fullmatch(
        r"\(CAST\(oi\.Qty AS decimal\(38, 6\)\) - ISNULL\(.*sri\.OrderItemID = oi\.ID.*, 0\)\)",
        rules.KEPT_UNITS,
    )
    assert "o.Created" not in sql
    assert "JOIN dbo.Sale" not in rules.SPINE and rules.SPINE.startswith("dbo.Sale s")
    assert "AT TIME ZONE 'FLE Standard Time'" in rules.SOLD_AT


def test_windows_are_whole_kyiv_days_as_utc_instants_clamped_to_the_floor():
    assert rules.utc_instant("2026-07-01") == datetime(2026, 6, 30, 21, 0)
    assert rules.utc_instant(date(2026, 11, 13)) == datetime(2026, 11, 12, 22, 0)
    assert rules.utc_instant("2026-11-13 10:30:00") == datetime(2026, 11, 13, 8, 30)
    assert rules.window("2026-11-13") == {
        "sold_from": datetime(2024, 12, 31, 22, 0),
        "sold_to": datetime(2026, 11, 12, 22, 0),
    }
    assert rules.trailing_window("2026-11-13", 365)["sold_from"] == datetime(2025, 11, 12, 22, 0)
    assert rules.trailing_window("2025-03-01", 365)["sold_from"] == datetime(2024, 12, 31, 22, 0)
    assert rules.window("2026-11-13", rules.trailing_year("2026-11-13"))["sold_from"] == datetime(
        2025, 11, 12, 22, 0
    )
    assert rules.trailing_year("2028-02-29") == datetime(2027, 2, 28)
    assert rules.forward_window("2026-10-13", 30) == {
        "sold_from": datetime(2026, 10, 12, 21, 0),
        "sold_to": datetime(2026, 11, 11, 22, 0),
    }


def test_default_as_of_is_the_kyiv_day():
    for module in (sales_repository, worker):
        assert "rules.today()" in inspect.getsource(module)
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", rules.today())


def test_owned_history_is_compared_on_live_catalog_identity():
    src = inspect.getsource(sales_repository.owned_live_product_ids)
    assert "historical.Deleted = 0" in src
    assert "current_product.VendorCode = historical.VendorCode" in src
    assert "ORDER BY current_product.ID DESC" in src


def test_model_and_cache_namespaces_include_history_floor():
    from app.data import cache
    from app.domain.models import RecommendationResult

    assert "20250101" in cache._MODEL_VERSION
    assert "20250101" in RecommendationResult.model_fields["model_version"].default
    assert "source_history_start_iso()" in inspect.getsource(als.get_model)


def test_stock_filter_is_scoped_to_operational_resale_storages():
    src = inspect.getsource(sales_repository.in_stock_product_ids)
    assert "JOIN dbo.Storage" in src
    assert 'rules.sellable_storage("s")' in src
    sellable = rules.sellable_storage("s")
    assert "s.Deleted = 0" in sellable
    assert "s.ForDefective = 0" in sellable
    assert "s.AvailableForReSale = 1 OR s.IsResale = 1" in sellable
    agreement = inspect.getsource(sales_repository._agreement_stock_product_ids)
    assert "s.ForDefective = 0" in agreement
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


def test_service_cards_are_excluded_with_the_synthetic_line():
    src = inspect.getsource(sales_repository.ubiquitous_product_ids)
    assert re.search(r"synthetic_product_ids\(\)\s*\|\s*service_product_ids\(\)\s*\|", src)
    lookup = inspect.getsource(sales_repository.service_product_ids)
    assert "NativeIsService = 1" in lookup and "Послуг%" in lookup and "послуга" in lookup
    assert "repo.service_product_ids()" in inspect.getsource(snapshot.load_data)


def test_synthetic_exclusion_is_unconditional_in_ubiquity():
    src = inspect.getsource(sales_repository.ubiquitous_product_ids)
    assert re.search(r"synthetic_product_ids\(\)\s*\|", src), (
        "synthetic ids must be UNION'd unconditionally (synthetic_product_ids() | <ubiquity set>), "
        "so the pinned exclusion is independent of the rolling ubiquity window"
    )


def test_precision_estimate_is_the_measured_value_for_the_served_model_version():
    from app.data import cache
    from app.domain.models import RecommendationResult
    from app.services.eval import measured

    stored = measured.load()
    assert stored["model_version"] == cache._MODEL_VERSION, (
        "measured.json was produced for another model version; rerun "
        "`python -m app.services.eval.holdout --write-measured`"
    )
    assert RecommendationResult.model_fields["model_version"].default == cache._MODEL_VERSION
    for kind in ("personal", "copurchase"):
        for k in (10, 25):
            value = measured.precision(kind, k)
            assert 0 < value < 1
            assert value not in (0.754, 0.033)
    assert measured.precision("personal", 7) == stored["precision"]["personal"]["10"]
    assert measured.precision("personal", 50) == stored["precision"]["personal"]["25"]
    result = RecommendationResult(
        customer_id=1, recommendations=[], count=0, discovery_count=0, segment="LIGHT",
        source_history_start="2025-01-01", effective_start="2025-01-01", history_complete=True,
    )
    assert result.precision_estimate == stored["precision"]["personal"]["25"]


def test_region_scoping_uses_region_id_natural_key_not_per_client_code():
    """byRegion scoping must group on dbo.Client.RegionID (the oblast, ~26 groups), NOT
    RegionCodeID (per-client address granularity that does not group)."""
    src = inspect.getsource(sales_repository.candidate_similar_customers)
    assert "RegionID" in src, "region scoping lost the Client.RegionID grouping key"
    assert "RegionCodeID" not in src, (
        "region scoping must NOT use the per-client RegionCodeID (it does not group clients)"
    )
