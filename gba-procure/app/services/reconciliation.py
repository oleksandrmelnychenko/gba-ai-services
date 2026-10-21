"""Read-only, executable reconciliation for the canonical procurement cart.

Inventory expectations use independent SQL. Purchase costs are independently counted
and calculated from raw verified observations, never from the production cohort median.
Buyer-approved unit-cost
overrides are read independently from Mongo and overlaid with the same precedence as
the policy. The module never writes to SQL Server, Mongo or Redis; callers supply a
plan factory (or an already loaded JSON plan).
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, localcontext
from enum import IntEnum
from fractions import Fraction
from typing import Any

from app.core.config import get_settings
from app.core.history import history_start_iso, rolling_coverage
from app.data import masters
from app.data import supply_repository as repo
from app.data.db import in_clause, query
from app.data.native_purchase_costs import (
    NativePurchaseCostClient,
    NativePurchaseCostError,
    PurchaseCostObservation,
    PurchaseCostRequest,
)
from app.data.purchase_cost_metadata import stable_manifest, validated_manifest
from app.data.synthetic import synthetic_product_id
from app.domain.models import MODEL_VERSION

_CENT = Decimal("0.01")
_FOUR_DECIMALS = Decimal("0.0001")
_QTY_TOLERANCE = Decimal("0.000001")
_INVENTORY_CHUNK = 800
_ON_ORDER_CHUNK = 400
_COST_HISTORY_DAYS = 540
_NO_OVERRIDE = object()


class ReconciliationExitCode(IntEnum):
    """Stable CLI exit contract."""

    EXACT = 0
    SOURCE_NOT_READY = 2
    DATA_MISMATCH = 3
    MONEY_OR_CONTRACT_MISMATCH = 4
    COVERAGE_GAP = 5
    INTERNAL_ERROR = 6


@dataclass(frozen=True)
class ReconciliationIssue:
    code: str
    category: str
    message: str
    severity: str = "error"
    key: dict[str, Any] = field(default_factory=dict)
    expected: Any = None
    actual: Any = None
    delta: Any = None

    def to_dict(self) -> dict[str, Any]:
        return _json_safe(
            {
                "code": self.code,
                "category": self.category,
                "severity": self.severity,
                "message": self.message,
                "key": self.key,
                "expected": self.expected,
                "actual": self.actual,
                "delta": self.delta,
            }
        )


@dataclass(frozen=True)
class InventoryFact:
    gross_on_hand: Decimal
    reserved: Decimal
    available: Decimal
    on_order: Decimal

    @property
    def position(self) -> Decimal:
        return self.available + self.on_order


@dataclass
class SourceFacts:
    inventory_by_product: dict[int, InventoryFact]
    cost_rows_by_pair: dict[tuple[int, int], list[Decimal]]
    availability_by_key: dict[tuple[int, int], Decimal]
    consignment_by_key: dict[tuple[int, int], Decimal]
    metrics: dict[str, Any]
    unit_cost_overrides_by_pair: dict[tuple[int, int], Any] = field(default_factory=dict)
    cost_observations: tuple[PurchaseCostObservation, ...] | None = None
    cost_manifest_matches: bool = True


@dataclass
class ReconciliationReport:
    as_of: str
    exit_code: ReconciliationExitCode
    issues: list[ReconciliationIssue]
    source_epoch_before: str | None
    source_epoch_after: str | None
    plan_digests: list[str]
    source_readiness: dict[str, Any]
    metrics: dict[str, Any]

    @property
    def ok(self) -> bool:
        return self.exit_code == ReconciliationExitCode.EXACT

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 2,
            "as_of": self.as_of,
            "ok": self.ok,
            "exit_code": int(self.exit_code),
            "exit_name": self.exit_code.name.lower(),
            "source_epoch_before": self.source_epoch_before,
            "source_epoch_after": self.source_epoch_after,
            "plan_digests": self.plan_digests,
            "source_readiness": _json_safe(self.source_readiness),
            "metrics": _json_safe(self.metrics),
            "issues": [issue.to_dict() for issue in self.issues],
        }


def _decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if value is None:
        return Decimal()
    return Decimal(str(value))


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def canonical_json_digest(payload: Mapping[str, Any]) -> str:
    """Digest an exact response, preserving list order while sorting object keys."""
    canonical = json.dumps(
        _json_safe(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _as_plan_dict(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return dict(value)
    raise TypeError("plan factory must return a mapping or Pydantic model")


def _item_ids(payload: Mapping[str, Any]) -> tuple[list[int], set[tuple[int, int]]]:
    product_ids: set[int] = set()
    pairs: set[tuple[int, int]] = set()
    items = payload.get("items")
    if not isinstance(items, list):
        return [], pairs
    for item in items:
        if not isinstance(item, Mapping):
            continue
        try:
            product_id = int(item.get("product_id"))
            producer_id = int(item.get("producer_id"))
        except (TypeError, ValueError):
            continue
        if product_id > 0:
            product_ids.add(product_id)
        if product_id > 0 and producer_id > 0:
            pairs.add((producer_id, product_id))
    return sorted(product_ids), pairs


def _rows_digest(rows: list[dict[str, Any]]) -> str:
    return canonical_json_digest({"rows": rows})


def collect_source_epoch(as_of: str, history_days: int) -> str:
    """Hash mutation-sensitive, compact watermarks before and after reconciliation."""
    coverage = rolling_coverage(as_of, history_days)
    syn = synthetic_product_id()
    supply = query(
        """
        WITH SupplyRows AS (
            SELECT CAST(0 AS tinyint) AS source_kind,
                   sioi.ID AS item_id,
                   sioi.ProductID AS product_id,
                   so.ClientID AS producer_id,
                   sioi.Qty AS qty,
                   sioi.UnitPrice AS unit_price,
                   sioi.Updated AS updated_at,
                   si.DateFrom AS source_date
            FROM dbo.SupplyInvoice si
            JOIN dbo.SupplyInvoiceOrderItem sioi
              ON sioi.SupplyInvoiceID = si.ID AND sioi.Deleted = 0
            JOIN dbo.SupplyOrder so ON so.ID = si.SupplyOrderID
            WHERE si.Deleted = 0
                  AND so.ClientID IS NOT NULL
                  AND sioi.ProductID IS NOT NULL
                  AND sioi.ProductID <> :syn

            UNION ALL

            SELECT CAST(1 AS tinyint) AS source_kind,
                   soui.ID AS item_id,
                   soui.ProductID AS product_id,
                   COALESCE(soui.SupplierID, sou.SupplierID) AS producer_id,
                   soui.Qty AS qty,
                   soui.UnitPrice AS unit_price,
                   soui.Updated AS updated_at,
                   sou.FromDate AS source_date
            FROM dbo.SupplyOrderUkraine sou
            JOIN dbo.SupplyOrderUkraineItem soui
              ON soui.SupplyOrderUkraineID = sou.ID AND soui.Deleted = 0
            WHERE sou.Deleted = 0
                  AND NOT (sou.IsFromCockpit = 1 AND sou.IsPlaced = 0)
                  AND COALESCE(soui.SupplierID, sou.SupplierID) IS NOT NULL
                  AND soui.ProductID IS NOT NULL
                  AND soui.ProductID <> :syn
        )
        SELECT COUNT_BIG(*) AS row_count,
               MAX(item_id) AS max_item_id,
               MAX(updated_at) AS max_updated_at,
               SUM(TRY_CONVERT(decimal(38,6), qty)) AS qty_sum,
               SUM(TRY_CONVERT(decimal(38,6), unit_price)) AS price_sum,
               CHECKSUM_AGG(BINARY_CHECKSUM(
                   source_kind, item_id, product_id, producer_id, qty, unit_price, updated_at
               )) AS row_checksum
        FROM SupplyRows
        WHERE source_date >= DATEADD(day, -:cost_days, :asof)
              AND source_date >= :history_start
              AND source_date < :asof
        """,
        {
            "asof": as_of,
            "cost_days": _COST_HISTORY_DAYS,
            "history_start": history_start_iso(),
            "syn": syn,
        },
    )
    demand = query(
        """
        SELECT COUNT_BIG(*) AS row_count,
               MAX(oi.ID) AS max_item_id,
               MAX(oi.Updated) AS max_updated_at,
               SUM(TRY_CONVERT(decimal(38,6), oi.Qty)) AS qty_sum,
               SUM(TRY_CONVERT(decimal(38,6), oi.PricePerItem)) AS price_sum,
               CHECKSUM_AGG(BINARY_CHECKSUM(
                   oi.ID, oi.ProductID, oi.Qty, oi.PricePerItem,
                   oi.IsValidForCurrentSale, oi.Updated
               )) AS row_checksum
        FROM dbo.[Order] o
        JOIN dbo.OrderItem oi ON oi.OrderID = o.ID
        WHERE oi.IsValidForCurrentSale = 1
              AND oi.ProductID <> :syn
              AND o.Created >= DATEADD(day, -:days, :asof)
              AND o.Created >= :history_start
              AND o.Created < :asof
        """,
        {
            "asof": as_of,
            "days": history_days,
            "history_start": history_start_iso(),
            "syn": syn,
        },
    )
    inventory = query(
        """
        WITH SellableStorage AS (
            SELECT st.ID
            FROM dbo.Storage st
            WHERE st.Deleted = 0
                  AND st.ForDefective = 0
                  AND (st.AvailableForReSale = 1 OR st.IsResale = 1)
        ),
        AvailabilityEpoch AS (
            SELECT COUNT_BIG(*) AS row_count,
                   MAX(pa.ID) AS max_item_id,
                   MAX(pa.Updated) AS max_updated_at,
                   SUM(TRY_CONVERT(decimal(38,6), pa.Amount)) AS qty_sum,
                   CHECKSUM_AGG(BINARY_CHECKSUM(
                       pa.ID, pa.ProductID, pa.StorageID, pa.Amount, pa.Updated
                   )) AS row_checksum
            FROM dbo.ProductAvailability pa
            JOIN SellableStorage ss ON ss.ID = pa.StorageID
            WHERE pa.Deleted = 0
        ),
        ReservationEpoch AS (
            SELECT COUNT_BIG(*) AS row_count,
                   MAX(pr.ID) AS max_item_id,
                   MAX(pr.Updated) AS max_updated_at,
                   SUM(TRY_CONVERT(decimal(38,6), pr.Qty)) AS qty_sum,
                   CHECKSUM_AGG(BINARY_CHECKSUM(
                       pr.ID, pr.ProductAvailabilityID, pr.Qty, pr.Updated
                   )) AS row_checksum
            FROM dbo.ProductReservation pr
            JOIN dbo.ProductAvailability pa ON pa.ID = pr.ProductAvailabilityID
            JOIN SellableStorage ss ON ss.ID = pa.StorageID
            WHERE pr.Deleted = 0 AND pa.Deleted = 0
        )
        SELECT a.row_count AS availability_rows,
               a.max_item_id AS max_availability_id,
               a.max_updated_at AS max_availability_updated,
               a.qty_sum AS availability_qty,
               a.row_checksum AS availability_checksum,
               r.row_count AS reservation_rows,
               r.max_item_id AS max_reservation_id,
               r.max_updated_at AS max_reservation_updated,
               r.qty_sum AS reservation_qty,
               r.row_checksum AS reservation_checksum
        FROM AvailabilityEpoch a
        CROSS JOIN ReservationEpoch r
        """,
    )
    receipts = query(
        """
        SELECT COUNT_BIG(*) AS row_count,
               MAX(pii.ID) AS max_item_id,
               MAX(pii.Updated) AS max_updated_at,
               SUM(TRY_CONVERT(decimal(38,6), pii.Qty)) AS qty_sum,
               CHECKSUM_AGG(BINARY_CHECKSUM(
                   pii.ID, pii.PackingListPackageOrderItemID,
                   pii.SupplyOrderUkraineItemID, pii.Qty, pii.Updated
               )) AS row_checksum
        FROM dbo.ProductIncomeItem pii
        JOIN dbo.ProductIncome pinc
          ON pinc.ID = pii.ProductIncomeID AND pinc.Deleted = 0
        WHERE pii.Deleted = 0
              AND pinc.FromDate >= :history_start
              AND pinc.FromDate < :asof
        """,
        {"asof": as_of, "history_start": history_start_iso()},
    )
    return _rows_digest(
        [
            {"kind": "supply", **(supply[0] if supply else {})},
            {"kind": "demand", **(demand[0] if demand else {})},
            {"kind": "inventory", **(inventory[0] if inventory else {})},
            {"kind": "receipts", **(receipts[0] if receipts else {})},
            {"kind": "history_contract", **coverage.as_metadata()},
        ]
    )


def _collect_availability(
    product_ids: list[int],
) -> tuple[dict[tuple[int, int], Decimal], dict[tuple[int, int], Decimal]]:
    availability: dict[tuple[int, int], Decimal] = {}
    reservations: dict[tuple[int, int], Decimal] = {}
    for start in range(0, len(product_ids), _INVENTORY_CHUNK):
        chunk = product_ids[start : start + _INVENTORY_CHUNK]
        placeholders, params = in_clause("p", chunk)
        for row in query(
            f"""
            SELECT pa.ProductID AS product_id,
                   pa.StorageID AS storage_id,
                   SUM(TRY_CONVERT(decimal(38,6), pa.Amount)) AS qty
            FROM dbo.ProductAvailability pa
            JOIN dbo.Storage st ON st.ID = pa.StorageID
            WHERE pa.Deleted = 0
                  AND pa.ProductID IN {placeholders}
                  AND st.Deleted = 0
                  AND st.ForDefective = 0
                  AND (st.AvailableForReSale = 1 OR st.IsResale = 1)
            GROUP BY pa.ProductID, pa.StorageID
            """,
            params,
        ):
            key = (int(row["product_id"]), int(row["storage_id"]))
            availability[key] = _decimal(row["qty"])
        for row in query(
            f"""
            SELECT pa.ProductID AS product_id,
                   pa.StorageID AS storage_id,
                   SUM(TRY_CONVERT(decimal(38,6), pr.Qty)) AS qty
            FROM dbo.ProductReservation pr
            JOIN dbo.ProductAvailability pa ON pa.ID = pr.ProductAvailabilityID
            JOIN dbo.Storage st ON st.ID = pa.StorageID
            WHERE pr.Deleted = 0
                  AND pa.Deleted = 0
                  AND pa.ProductID IN {placeholders}
                  AND st.Deleted = 0
                  AND st.ForDefective = 0
                  AND (st.AvailableForReSale = 1 OR st.IsResale = 1)
            GROUP BY pa.ProductID, pa.StorageID
            """,
            params,
        ):
            key = (int(row["product_id"]), int(row["storage_id"]))
            reservations[key] = _decimal(row["qty"])
    return availability, reservations


def _collect_consignments(product_ids: list[int]) -> dict[tuple[int, int], Decimal]:
    consignments: dict[tuple[int, int], Decimal] = {}
    for start in range(0, len(product_ids), _INVENTORY_CHUNK):
        chunk = product_ids[start : start + _INVENTORY_CHUNK]
        placeholders, params = in_clause("p", chunk)
        rows = query(
            f"""
            SELECT ci.ProductID AS product_id,
                   c.StorageID AS storage_id,
                   SUM(TRY_CONVERT(decimal(38,6), ci.RemainingQty)) AS qty
            FROM dbo.ConsignmentItem ci
            JOIN dbo.Consignment c ON c.ID = ci.ConsignmentID
            JOIN dbo.Storage st ON st.ID = c.StorageID
            WHERE ci.Deleted = 0
                  AND c.Deleted = 0
                  AND ci.ProductID IN {placeholders}
                  AND st.Deleted = 0
                  AND st.ForDefective = 0
                  AND (st.AvailableForReSale = 1 OR st.IsResale = 1)
            GROUP BY ci.ProductID, c.StorageID
            """,
            params,
        )
        for row in rows:
            key = (int(row["product_id"]), int(row["storage_id"]))
            consignments[key] = _decimal(row["qty"])
    return consignments


def _collect_on_order(product_ids: list[int], as_of: str) -> dict[int, Decimal]:
    on_order: dict[int, Decimal] = {}
    syn = synthetic_product_id()
    for start in range(0, len(product_ids), _ON_ORDER_CHUNK):
        chunk = product_ids[start : start + _ON_ORDER_CHUNK]
        placeholders, params = in_clause("p", chunk)
        rows = query(
            f"""
            WITH OrderedQty AS (
                SELECT sioi.ProductID AS product_id,
                       SUM(TRY_CONVERT(decimal(38,6), plpoi.Qty)) AS qty
                FROM dbo.PackingListPackageOrderItem plpoi
                JOIN dbo.SupplyInvoiceOrderItem sioi
                  ON sioi.ID = plpoi.SupplyInvoiceOrderItemID AND sioi.Deleted = 0
                JOIN dbo.PackingList pl
                  ON pl.ID = plpoi.PackingListID AND pl.Deleted = 0
                JOIN dbo.SupplyInvoice si
                  ON si.ID = pl.SupplyInvoiceID AND si.Deleted = 0
                WHERE plpoi.Deleted = 0
                      AND sioi.ProductID <> :syn
                      AND sioi.ProductID IN {placeholders}
                      AND si.DateFrom >= :history_start
                      AND si.DateFrom < :asof
                GROUP BY sioi.ProductID

                UNION ALL

                SELECT soui.ProductID AS product_id,
                       SUM(TRY_CONVERT(decimal(38,6), soui.Qty)) AS qty
                FROM dbo.SupplyOrderUkraineItem soui
                JOIN dbo.SupplyOrderUkraine sou
                  ON sou.ID = soui.SupplyOrderUkraineID AND sou.Deleted = 0
                WHERE soui.Deleted = 0
                      AND NOT (sou.IsFromCockpit = 1 AND sou.IsPlaced = 0)
                      AND soui.ProductID <> :syn
                      AND soui.ProductID IN {placeholders}
                      AND sou.FromDate >= :history_start
                      AND sou.FromDate < :asof
                GROUP BY soui.ProductID
            ),
            ReceivedQty AS (
                SELECT sioi.ProductID AS product_id,
                       SUM(TRY_CONVERT(decimal(38,6), pii.Qty)) AS qty
                FROM dbo.ProductIncomeItem pii
                JOIN dbo.ProductIncome pinc
                  ON pinc.ID = pii.ProductIncomeID AND pinc.Deleted = 0
                JOIN dbo.PackingListPackageOrderItem plpoi
                  ON plpoi.ID = pii.PackingListPackageOrderItemID AND plpoi.Deleted = 0
                JOIN dbo.SupplyInvoiceOrderItem sioi
                  ON sioi.ID = plpoi.SupplyInvoiceOrderItemID AND sioi.Deleted = 0
                WHERE pii.Deleted = 0
                      AND sioi.ProductID <> :syn
                      AND sioi.ProductID IN {placeholders}
                      AND pinc.FromDate >= :history_start
                      AND pinc.FromDate < :asof
                GROUP BY sioi.ProductID

                UNION ALL

                SELECT soui.ProductID AS product_id,
                       SUM(TRY_CONVERT(decimal(38,6), pii.Qty)) AS qty
                FROM dbo.ProductIncomeItem pii
                JOIN dbo.ProductIncome pinc
                  ON pinc.ID = pii.ProductIncomeID AND pinc.Deleted = 0
                JOIN dbo.SupplyOrderUkraineItem soui
                  ON soui.ID = pii.SupplyOrderUkraineItemID AND soui.Deleted = 0
                JOIN dbo.SupplyOrderUkraine sou
                  ON sou.ID = soui.SupplyOrderUkraineID AND sou.Deleted = 0
                WHERE pii.Deleted = 0
                      AND NOT (sou.IsFromCockpit = 1 AND sou.IsPlaced = 0)
                      AND soui.ProductID <> :syn
                      AND soui.ProductID IN {placeholders}
                      AND pinc.FromDate >= :history_start
                      AND pinc.FromDate < :asof
                GROUP BY soui.ProductID
            ),
            OrderedByProduct AS (
                SELECT product_id, SUM(qty) AS qty
                FROM OrderedQty
                GROUP BY product_id
            ),
            ReceivedByProduct AS (
                SELECT product_id, SUM(qty) AS qty
                FROM ReceivedQty
                GROUP BY product_id
            )
            SELECT ordered.product_id,
                   ordered.qty - ISNULL(received.qty, 0) AS qty
            FROM OrderedByProduct ordered
            LEFT JOIN ReceivedByProduct received
              ON received.product_id = ordered.product_id
            WHERE ordered.qty - ISNULL(received.qty, 0) > 0.000001
            """,
            {
                "asof": as_of,
                "history_start": history_start_iso(),
                "syn": syn,
                **params,
            },
        )
        for row in rows:
            on_order[int(row["product_id"])] = _decimal(row["qty"])
    return on_order


def _collect_cost_evidence(payload: Mapping[str, Any], as_of: str):
    """Re-read exact published scopes, then independently retain raw known observations.

    This does not invoke the production cohort aggregate or the old currency UDF.
    A changed whole-document fingerprint is source_changed, not an arithmetic defect.
    """
    if "cost_observation_manifest" not in payload:
        raise NativePurchaseCostError("missing_purchase_cost_manifest")
    try:
        manifest = validated_manifest(payload["cost_observation_manifest"], as_of)
    except (TypeError, ValueError, KeyError):
        raise NativePurchaseCostError("invalid_purchase_cost_manifest") from None
    client = NativePurchaseCostClient.from_settings(get_settings()) if manifest.components else None
    observations = []
    scopes = set()
    identities = set()
    unchanged = True
    for component in manifest.components:
        if component.as_of_exclusive != as_of or scopes.intersection(component.product_ids):
            raise NativePurchaseCostError("purchase_cost_manifest_scope_mismatch")
        scopes.update(component.product_ids)
        response = client.read(
            PurchaseCostRequest(
                tuple(component.product_ids),
                component.supplier_id,
                date.fromisoformat(as_of),
                component.history_days,
            )
        )
        unchanged = unchanged and response.fingerprint == component.fingerprint
        for row in response.observations:
            if row.observation_id in identities:
                raise NativePurchaseCostError("duplicate_purchase_cost_observation")
            identities.add(row.observation_id)
            observations.append(row)
        if len(observations) > 50_000:
            raise NativePurchaseCostError("purchase_cost_aggregation_too_large")
    products, _ = _item_ids(payload)
    if not set(products).issubset(scopes):
        raise NativePurchaseCostError("purchase_cost_manifest_scope_mismatch")
    costs = defaultdict(list)
    known_units = defaultdict(set)
    for row in observations:
        if row.status == "known":
            costs[row.supplier_id, row.product_id].append(row.unit_net_cost_eur)
            known_units[row.supplier_id, row.product_id].add(row.unit_id)
    # A native unit change cannot be hidden by combining unit prices into one scalar.
    return (
        {pair: values for pair, values in costs.items() if len(known_units[pair]) == 1},
        tuple(observations),
        unchanged,
    )


def _collect_unit_cost_overrides(
    selected_pairs: set[tuple[int, int]],
) -> dict[tuple[int, int], Any]:
    """Read the exact buyer override fields used by the policy for selected lines."""
    if not selected_pairs or not get_settings().use_masters:
        return {}

    product_ids_by_producer: defaultdict[int, list[int]] = defaultdict(list)
    for producer_id, product_id in sorted(selected_pairs):
        product_ids_by_producer[producer_id].append(product_id)

    overrides: dict[tuple[int, int], Any] = {}
    for producer_id, product_ids in product_ids_by_producer.items():
        terms_by_product = masters.product_terms_for(producer_id, product_ids, required=True)
        for product_id in product_ids:
            terms = terms_by_product.get(product_id)
            if isinstance(terms, Mapping) and "unit_cost_override" in terms:
                overrides[(producer_id, product_id)] = terms["unit_cost_override"]
    return overrides


def collect_source_facts(payload: Mapping[str, Any], as_of: str) -> SourceFacts:
    """Collect exact expected values for only the products returned by the cart."""
    product_ids, selected_pairs = _item_ids(payload)
    availability, reservations = _collect_availability(product_ids)
    consignments = _collect_consignments(product_ids)
    on_order = _collect_on_order(product_ids, as_of)
    cost_rows, cost_observations, cost_manifest_matches = _collect_cost_evidence(payload, as_of)
    unit_cost_overrides = _collect_unit_cost_overrides(selected_pairs)

    inventory: dict[int, InventoryFact] = {}
    for product_id in product_ids:
        storage_keys = {key for key in set(availability) | set(reservations) if key[0] == product_id}
        available = sum((availability.get(key, Decimal()) for key in storage_keys), Decimal())
        reserved = sum((reservations.get(key, Decimal()) for key in storage_keys), Decimal())
        inventory[product_id] = InventoryFact(
            gross_on_hand=available + reserved,
            reserved=reserved,
            available=available,
            on_order=on_order.get(product_id, Decimal()),
        )

    selected_cost_rows = {pair: values for pair, values in cost_rows.items() if pair in selected_pairs}
    metrics = {
        "products_checked": len(product_ids),
        "producer_product_pairs_checked": len(selected_pairs),
        "products_with_inventory": len({key[0] for key in availability}),
        "products_with_reservations": sum(1 for fact in inventory.values() if fact.reserved != 0),
        "products_with_on_order": sum(1 for fact in inventory.values() if fact.on_order != 0),
        "priced_selected_pairs": len(selected_cost_rows),
        "unit_cost_override_rows": len(unit_cost_overrides),
        "availability_storage_keys": len(availability),
        "consignment_storage_keys": len(consignments),
    }
    return SourceFacts(
        inventory_by_product=inventory,
        cost_rows_by_pair=selected_cost_rows,
        availability_by_key=availability,
        consignment_by_key=consignments,
        metrics=metrics,
        unit_cost_overrides_by_pair=unit_cost_overrides,
        cost_observations=cost_observations,
        cost_manifest_matches=cost_manifest_matches,
    )


def _issue(
    issues: list[ReconciliationIssue],
    code: str,
    category: str,
    message: str,
    *,
    key: dict[str, Any] | None = None,
    expected: Any = None,
    actual: Any = None,
    delta: Any = None,
    severity: str = "error",
) -> None:
    issues.append(
        ReconciliationIssue(
            code=code,
            category=category,
            message=message,
            severity=severity,
            key=key or {},
            expected=expected,
            actual=actual,
            delta=delta,
        )
    )


def _compare_decimal(
    issues: list[ReconciliationIssue],
    *,
    code: str,
    category: str,
    message: str,
    key: dict[str, Any],
    expected: Decimal,
    actual: Any,
    tolerance: Decimal = Decimal(),
) -> None:
    try:
        if actual is None or isinstance(actual, bool):
            raise ValueError("missing numeric value")
        actual_decimal = _decimal(actual)
        if not actual_decimal.is_finite():
            raise ValueError("nonfinite numeric value")
    except Exception:  # noqa: BLE001
        _issue(
            issues,
            code,
            category,
            message,
            key=key,
            expected=expected,
            actual=actual,
        )
        return
    delta = actual_decimal - expected
    if abs(delta) > tolerance:
        _issue(
            issues,
            code,
            category,
            message,
            key=key,
            expected=expected,
            actual=actual_decimal,
            delta=delta,
        )


def _round_exact(value: Fraction, scale: int) -> Decimal:
    scaled = value * 10**scale
    whole, remainder = divmod(scaled.numerator, scaled.denominator)
    units = whole + (2 * remainder >= scaled.denominator)
    return Decimal(f"{units // 10**scale}.{units % 10**scale:0{scale}d}")


def _fraction_decimal(value: Fraction) -> Decimal:
    with localcontext() as context:
        context.prec = max(28, len(str(abs(value.numerator))) + value.denominator.bit_length())
        result = Decimal(value.numerator) / Decimal(value.denominator)
    if Fraction(result) != value:
        raise ValueError("unsupported exact decimal quantity")
    return result


def _exact_median(values: list[Decimal]) -> Decimal:
    ordered = sorted(Fraction(value) for value in values)
    middle = len(ordered) // 2
    value = ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2
    return _round_exact(value, 4)


def _valid_unit_cost_override(value: Any) -> Decimal | None:
    """Return a policy-applicable override, rejecting dirty Mongo values."""
    if isinstance(value, bool):
        return None
    try:
        # Policy serves the Mongo value as a float; reproduce that public value
        # before doing exact Decimal multiplication for the independent audit.
        override = Decimal(str(float(value)))
    except (TypeError, ValueError, OverflowError):
        return None
    if not override.is_finite() or override <= 0:
        return None
    return override


def _independent_provenance(
    rows: list[PurchaseCostObservation], override: Decimal | None, quantity: Decimal, refs: list[str]
) -> dict:
    """Independent raw-observation statistic; never calls the production cohort aggregate."""
    known = [row for row in rows if row.status == "known"]
    units = sorted({row.unit_id for row in known})
    agreements = sorted({row.supplier_client_agreement_id for row in known})
    counts = Counter(reason for row in rows for reason in row.reason_codes)
    coverage = (
        "no_observations"
        if not rows
        else ("complete" if len(known) == len(rows) else "partial" if known else "unknown")
    )
    median = _exact_median([row.unit_net_cost_eur for row in known]) if known and len(units) == 1 else None
    aggregation = "mixed_current_units" if len(units) > 1 else None
    usable = median is not None and Decimal(str(float(median))) == median
    if median is not None and not usable:
        aggregation = "cost_float_precision_unsupported"
    exclusions = []
    if quantity <= 0:
        exclusions.append("no_positive_suggested_quantity")
    if coverage != "complete":
        exclusions.append("no_observations" if not rows else "incomplete_observation_coverage")
    if aggregation:
        exclusions.append(aggregation)
    if not usable:
        exclusions.append("verified_cost_unavailable")
    else:
        line = _round_exact(Fraction(median) * Fraction(quantity), 2)
        if Decimal(str(float(line))) != line:
            exclusions.append("line_amount_precision_unsupported")
    expected = {
        "version": 1,
        "source": "receipt_history",
        "scope": "CurrentPostedReceipts",
        "resolver_version": "current-posted-receipt-cost-v1",
        "basis": "net_goods_excluding_vat_delivery_customs",
        "statistic": "observation_median_half_up_4",
        "canonical_unit_cost_eur": format(median, ".4f") if median is not None else None,
        "current_unit_ids": units,
        "supplier_client_agreement_ids": agreements,
        "observation_count": len(rows),
        "known_count": len(known),
        "unknown_count": len(rows) - len(known),
        "coverage": coverage,
        "reason_counts": [{"reason": key, "count": counts[key]} for key in sorted(counts)],
        "aggregation_reason": aggregation,
        "component_refs": refs,
        "budget_eligible": not exclusions,
        "budget_exclusion_reasons": exclusions,
    }
    if override is not None:
        expected.update(
            source="buyer_supplied",
            scope="BuyerSupplied",
            resolver_version=None,
            basis="buyer_tax_basis_unverified",
            statistic="buyer_override",
            canonical_unit_cost_eur=format(override, "f"),
            current_unit_ids=[],
            supplier_client_agreement_ids=[],
            observation_count=0,
            known_count=0,
            unknown_count=0,
            coverage="buyer_supplied",
            reason_counts=[],
            aggregation_reason=None,
            component_refs=[],
            budget_eligible=False,
            budget_exclusion_reasons=["buyer_tax_basis_unverified"]
            + (["no_positive_suggested_quantity"] if quantity <= 0 else []),
        )
    return expected


def _validate_purchase_contract(
    payload: Mapping[str, Any], facts: SourceFacts, issues: list[ReconciliationIssue]
) -> dict:
    if (
        facts.cost_observations is None
    ):  # Explicit synthetic legacy arithmetic fixture, never production loader.
        return {}
    by_pair = defaultdict(list)
    for row in facts.cost_observations:
        by_pair[row.supplier_id, row.product_id].append(row)
    components = payload["cost_observation_manifest"]["components"]
    refs = {pid: [component["component_id"]] for component in components for pid in component["product_ids"]}
    coverages = Counter()
    eligible_count = 0
    selected_score = Fraction()
    selected_cost = Fraction()
    selected_count = 0
    for item in payload["items"]:
        product, supplier = item["product_id"], item["producer_id"]
        quantity = _decimal(item["suggested_qty"])
        raw_override = facts.unit_cost_overrides_by_pair.get((supplier, product))
        override = _valid_unit_cost_override(raw_override) if raw_override is not None else None
        expected = _independent_provenance(
            by_pair[supplier, product], override, quantity, refs.get(product, [])
        )
        coverages[expected["coverage"]] += 1
        eligible_count += expected["budget_eligible"]
        if item.get("cost_provenance") != expected:
            _issue(
                issues,
                "C020",
                "contract",
                "cost provenance differs from independently counted observations",
                key={"product_id": product, "producer_id": supplier},
                expected=expected,
                actual=item.get("cost_provenance"),
            )
        weight = (
            {
                "critical": Decimal("1"),
                "high": Decimal("0.7"),
                "normal": Decimal("0.4"),
                "none": Decimal("0.1"),
            }[item["urgency"]]
            if quantity > 0
            else Decimal()
        )
        if (
            item.get("unit_margin_eur") is not None
            or item.get("value_density") is not None
            or item.get("margin_unavailable_reason") != "sale_tax_basis_unverified"
            or item.get("service_level_basis") != "abc_fallback_with_optional_producer_floor"
            or item.get("sale_price_basis")
            != (
                "unavailable"
                if item.get("unit_sale_eur") is None
                else "historical_recorded_tax_basis_unverified"
            )
            or _decimal(item.get("budget_priority_weight")) != weight
        ):
            _issue(
                issues, "C021", "contract", "sale tax basis or dimensionless priority metadata inconsistent"
            )
        if item.get("within_budget") is True:
            selected_count += 1
            if not expected["budget_eligible"]:
                _issue(
                    issues, "C022", "contract", "unverified or nonpositive-demand line selected for budget"
                )
            selected_score += Fraction(weight)
            if item.get("line_cost_eur") is not None:
                selected_cost += Fraction(_decimal(item["line_cost_eur"]))
        alt = item.get("cheaper_alt")
        if alt is not None:
            alt_expected = _independent_provenance(
                by_pair[alt["producer_id"], product], None, quantity, refs.get(product, [])
            )
            alt_cost = alt_expected["canonical_unit_cost_eur"]
            base_cost = expected["canonical_unit_cost_eur"]
            comparable = (
                expected["source"] == "receipt_history"
                and base_cost is not None
                and alt_cost is not None
                and item.get("unit_cost_eur") is not None
                and Fraction(Decimal(alt_cost)) < Fraction(Decimal(base_cost))
                and Fraction(Decimal(alt_cost)) * 50 <= Fraction(Decimal(base_cost)) * 49
            )
            if (
                not comparable
                or alt.get("cost_provenance") != alt_expected
                or alt.get("comparison_basis") != "historical_net_goods"
                or alt_cost is None
                or _decimal(alt.get("cost_eur")) != Decimal(alt_cost)
            ):
                _issue(
                    issues, "C025", "contract", "alternative historical cost differs from raw observations"
                )
    total_certified = eligible_count == len(payload["items"])
    basis = (
        "includes_buyer_values_with_unverified_tax_basis"
        if coverages["buyer_supplied"]
        else "net_goods_excluding_vat_delivery_customs"
    )
    if (
        payload.get("cost_totals_certified") != total_certified
        or payload.get("cost_total_basis") != basis
        or payload.get("history_scope") != "demand"
        or payload.get("value_captured_eur") is not None
    ):
        _issue(issues, "C023", "contract", "underlying plan cost scope or certification is inconsistent")
    if payload.get("budget_eur") is not None and (
        payload.get("budget_objective") != "urgency_weighted_lines"
        or payload.get("budget_score") is None
        or payload.get("budget_used_eur") is None
        or _decimal(payload.get("budget_score")) != _fraction_decimal(selected_score)
        or _decimal(payload.get("budget_used_eur")) != _fraction_decimal(selected_cost)
        or selected_cost > Fraction(_decimal(payload["budget_eur"]))
        or payload.get("selected_count") != selected_count
        or payload.get("deferred_count") != len(payload["items"]) - selected_count
    ):
        _issue(issues, "C024", "contract", "budget score or verified selected cost is inconsistent")
    known_count = sum(row.status == "known" for row in facts.cost_observations)
    return {
        "scope": "CurrentPostedReceipts",
        "resolver_version": "current-posted-receipt-cost-v1",
        "basis": "net_goods_excluding_vat_delivery_customs",
        "components": [
            {
                key: component[key]
                for key in (
                    "component_id",
                    "product_ids",
                    "supplier_id",
                    "as_of_exclusive",
                    "history_days",
                    "fingerprint",
                )
            }
            for component in components
        ],
        "observation_count": len(facts.cost_observations),
        "known_count": known_count,
        "unknown_count": len(facts.cost_observations) - known_count,
        "selected_pair_count": len(payload["items"]),
        "complete_pair_count": coverages["complete"],
        "partial_pair_count": coverages["partial"],
        "unknown_pair_count": coverages["unknown"],
        "no_observations_pair_count": coverages["no_observations"],
        "buyer_supplied_pair_count": coverages["buyer_supplied"],
        "budget_eligible_pair_count": eligible_count,
        "cost_totals_certified": total_certified,
    }


def validate_canonical_plan(
    payload: Mapping[str, Any],
    facts: SourceFacts,
    as_of: str,
    *,
    strict_coverage: bool = False,
) -> tuple[list[ReconciliationIssue], dict[str, Any]]:
    """Validate canonical metadata and every returned product, quantity and cent."""
    issues: list[ReconciliationIssue] = []
    items = payload.get("items")
    if not isinstance(items, list):
        _issue(issues, "C001", "contract", "items must be a list", actual=items)
        return issues, {}

    item_count = payload.get("item_count")
    if item_count != len(items):
        _issue(
            issues,
            "C002",
            "contract",
            "item_count must equal the response item length",
            expected=len(items),
            actual=item_count,
        )
    total_item_count = payload.get("total_item_count")
    if total_item_count != len(items):
        _issue(
            issues,
            "C003",
            "contract",
            "canonical total_item_count must equal the untruncated item length",
            expected=len(items),
            actual=total_item_count,
        )
    if payload.get("is_truncated") is not False:
        _issue(
            issues,
            "C004",
            "contract",
            "canonical cart must not be truncated",
            expected=False,
            actual=payload.get("is_truncated"),
        )
    if payload.get("as_of_date") != as_of:
        _issue(
            issues,
            "C005",
            "contract",
            "plan date must equal the audited date",
            expected=as_of,
            actual=payload.get("as_of_date"),
        )
    expected_history = rolling_coverage(as_of, get_settings().history_days).as_metadata()
    actual_history = {key: payload.get(key) for key in expected_history}
    if actual_history != expected_history:
        _issue(
            issues,
            "C011",
            "contract",
            "plan history coverage metadata must match the source floor",
            expected=expected_history,
            actual=actual_history,
        )
    if payload.get("history_not_applicable") != ["inventory", "reservations", "purchase_costs"]:
        _issue(
            issues,
            "C012",
            "contract",
            "current inventory and reservations must be explicit history N/A inputs",
            expected=["inventory", "reservations", "purchase_costs"],
            actual=payload.get("history_not_applicable"),
        )
    if payload.get("model_version") != MODEL_VERSION:
        _issue(
            issues,
            "C013",
            "contract",
            "plan model version must identify the active source-history floor",
            expected=MODEL_VERSION,
            actual=payload.get("model_version"),
        )

    product_occurrences: defaultdict[int, int] = defaultdict(int)
    total_qty = Fraction()
    priced_total = Fraction()
    computed_unpriced = 0
    applied_unit_cost_overrides = 0
    invalid_unit_cost_overrides = 0

    for index, raw_item in enumerate(items):
        if not isinstance(raw_item, Mapping):
            _issue(
                issues,
                "C006",
                "contract",
                "each item must be an object",
                key={"index": index},
                actual=raw_item,
            )
            continue
        try:
            product_id = int(raw_item.get("product_id"))
            producer_id = int(raw_item.get("producer_id"))
        except (TypeError, ValueError):
            _issue(
                issues,
                "C007",
                "contract",
                "product_id and producer_id must be positive integers",
                key={"index": index},
                actual={
                    "product_id": raw_item.get("product_id"),
                    "producer_id": raw_item.get("producer_id"),
                },
            )
            continue
        key = {
            "index": index,
            "producer_id": producer_id,
            "product_id": product_id,
        }
        if product_id <= 0 or producer_id <= 0:
            _issue(
                issues,
                "C007",
                "contract",
                "product_id and producer_id must be positive integers",
                key=key,
            )
            continue

        product_occurrences[product_id] += 1
        forecast = raw_item.get("forecast")
        inventory = raw_item.get("inventory")
        if not isinstance(forecast, Mapping) or forecast.get("product_id") != product_id:
            _issue(
                issues,
                "C008",
                "contract",
                "nested forecast.product_id must match item.product_id",
                key=key,
                expected=product_id,
                actual=forecast.get("product_id") if isinstance(forecast, Mapping) else None,
            )
        if not isinstance(inventory, Mapping) or inventory.get("product_id") != product_id:
            _issue(
                issues,
                "C009",
                "contract",
                "nested inventory.product_id must match item.product_id",
                key=key,
                expected=product_id,
                actual=inventory.get("product_id") if isinstance(inventory, Mapping) else None,
            )

        try:
            suggested_qty = _decimal(raw_item.get("suggested_qty"))
        except Exception:  # noqa: BLE001
            suggested_qty = Decimal()
            _issue(
                issues,
                "Q001",
                "quantity",
                "suggested_qty must be numeric",
                key=key,
                actual=raw_item.get("suggested_qty"),
            )
        if suggested_qty <= 0:
            _issue(
                issues,
                "Q002",
                "quantity",
                "canonical needed-only item must have positive suggested_qty",
                key=key,
                actual=suggested_qty,
            )
        total_qty += Fraction(suggested_qty)

        fact = facts.inventory_by_product.get(product_id)
        if fact is None:
            _issue(
                issues,
                "Q003",
                "quantity",
                "source inventory facts are missing for returned product",
                key=key,
            )
        elif isinstance(inventory, Mapping):
            for field_name, expected in (
                ("on_hand", fact.gross_on_hand),
                ("reserved", fact.reserved),
                ("available", fact.available),
                ("on_order", fact.on_order),
                ("position", fact.position),
            ):
                _compare_decimal(
                    issues,
                    code="Q004",
                    category="quantity",
                    message=f"inventory.{field_name} differs from independent SQL",
                    key={**key, "field": field_name},
                    expected=expected,
                    actual=inventory.get(field_name),
                    tolerance=_QTY_TOLERANCE,
                )

        unit_cost = raw_item.get("unit_cost_eur")
        line_cost = raw_item.get("line_cost_eur")
        pair = (producer_id, product_id)
        source_costs = facts.cost_rows_by_pair.get(pair, [])
        raw_override = facts.unit_cost_overrides_by_pair.get(pair, _NO_OVERRIDE)
        expected_unit_cost: Decimal | None = None
        expected_cost_source = "exact Decimal median"
        if raw_override is not _NO_OVERRIDE:
            expected_unit_cost = _valid_unit_cost_override(raw_override)
            zero_override = (
                type(raw_override) in (int, float, Decimal)
                and _decimal(raw_override).is_finite()
                and _decimal(raw_override) == 0
            )
            if expected_unit_cost is None and not zero_override:
                invalid_unit_cost_overrides += 1
                _issue(
                    issues,
                    "M008",
                    "money",
                    "unit_cost_override must be a finite positive number",
                    key=key,
                    expected="finite value > 0",
                    actual=raw_override,
                )
            elif expected_unit_cost is not None:
                applied_unit_cost_overrides += 1
                expected_cost_source = "buyer unit_cost_override"

        if expected_unit_cost is None and source_costs:
            expected_unit_cost = _exact_median(source_costs)
            if Decimal(str(float(expected_unit_cost))) != expected_unit_cost:
                expected_unit_cost = None
        if expected_unit_cost is None:
            computed_unpriced += 1
            if unit_cost is not None or line_cost is not None:
                _issue(
                    issues,
                    "M001",
                    "money",
                    "unconfirmed cost must remain unknown",
                    key=key,
                    actual={"unit_cost_eur": unit_cost, "line_cost_eur": line_cost},
                )
            else:
                _issue(
                    issues,
                    "G003",
                    "coverage",
                    "item has no usable verified cost scalar",
                    key=key,
                    severity="warning",
                )
            continue

        _compare_decimal(
            issues,
            code="M002",
            category="money",
            message=f"unit_cost_eur differs from the resolved {expected_cost_source}",
            key={**key, "cost_source": expected_cost_source},
            expected=expected_unit_cost,
            actual=unit_cost,
        )
        expected_line_cost = _round_exact(Fraction(expected_unit_cost) * Fraction(suggested_qty), 2)
        if Decimal(str(float(expected_line_cost))) != expected_line_cost:
            computed_unpriced += 1
            if line_cost is not None:
                _issue(issues, "M003", "money", "unrepresentable line cents must remain unknown", key=key)
            continue
        _compare_decimal(
            issues,
            code="M003",
            category="money",
            message="line_cost_eur differs from exact unit cost × quantity",
            key=key,
            expected=expected_line_cost,
            actual=line_cost,
        )
        priced_total += Fraction(expected_line_cost)

    duplicate_products = {product_id: count for product_id, count in product_occurrences.items() if count > 1}
    if duplicate_products:
        _issue(
            issues,
            "C010",
            "contract",
            "canonical cart must contain one supplier option per product",
            expected="unique product_id",
            actual=duplicate_products,
        )

    for storage_key in sorted(set(facts.availability_by_key) | set(facts.consignment_by_key)):
        available = facts.availability_by_key.get(storage_key, Decimal())
        remaining = facts.consignment_by_key.get(storage_key, Decimal())
        if abs(available - remaining) > _QTY_TOLERANCE:
            _issue(
                issues,
                "Q005",
                "quantity",
                "ProductAvailability differs from ConsignmentItem.RemainingQty",
                key={
                    "product_id": storage_key[0],
                    "storage_id": storage_key[1],
                },
                expected=remaining,
                actual=available,
                delta=available - remaining,
            )

    expected_total_qty = _fraction_decimal(total_qty)
    priced_total = _fraction_decimal(priced_total)
    _compare_decimal(
        issues,
        code="M004",
        category="money",
        message="total_suggested_qty differs from the sum of item quantities",
        key={},
        expected=expected_total_qty,
        actual=payload.get("total_suggested_qty"),
    )
    _compare_decimal(
        issues,
        code="M005",
        category="money",
        message="priced_cost_eur differs from the sum of exact line cents",
        key={},
        expected=priced_total,
        actual=payload.get("priced_cost_eur"),
    )
    if computed_unpriced == 0:
        _compare_decimal(
            issues,
            code="M006",
            category="money",
            message="total_cost_eur differs from the sum of exact line cents",
            key={},
            expected=priced_total,
            actual=payload.get("total_cost_eur"),
        )
    if computed_unpriced and payload.get("total_cost_eur") is not None:
        _issue(issues, "M006", "money", "total_cost_eur must be unknown when any line is unpriced")
    if payload.get("unpriced_item_count") != computed_unpriced:
        _issue(
            issues,
            "M007",
            "money",
            "unpriced_item_count differs from independently priced lines",
            expected=computed_unpriced,
            actual=payload.get("unpriced_item_count"),
        )

    if facts.metrics.get("products_with_reservations", 0) <= 0:
        _issue(
            issues,
            "G001",
            "coverage",
            "live cart has no non-zero reservation example",
            severity="error" if strict_coverage else "warning",
        )
    if facts.metrics.get("products_with_on_order", 0) <= 0:
        _issue(
            issues,
            "G002",
            "coverage",
            "live cart has no non-zero in-transit example",
            severity="error" if strict_coverage else "warning",
        )

    cost_metrics = _validate_purchase_contract(payload, facts, issues)
    if cost_metrics:
        cost_metrics.update(
            priced_selected_pairs=len(items) - computed_unpriced,
            computed_unpriced_items=computed_unpriced,
            computed_priced_cost_eur=priced_total,
        )
    metrics = {
        **facts.metrics,
        "purchase_costs": cost_metrics,
        "plan_items": len(items),
        "unique_plan_products": len(product_occurrences),
        "computed_total_suggested_qty": expected_total_qty,
        "computed_priced_cost_eur": priced_total,
        "computed_unpriced_items": computed_unpriced,
        "applied_unit_cost_overrides": applied_unit_cost_overrides,
        "invalid_unit_cost_overrides": invalid_unit_cost_overrides,
        "consignment_drift_keys": sum(
            1
            for storage_key in set(facts.availability_by_key) | set(facts.consignment_by_key)
            if abs(
                facts.availability_by_key.get(storage_key, Decimal())
                - facts.consignment_by_key.get(storage_key, Decimal())
            )
            > _QTY_TOLERANCE
        ),
    }
    return issues, metrics


def exit_code_for_issues(
    issues: list[ReconciliationIssue],
) -> ReconciliationExitCode:
    errors = [issue for issue in issues if issue.severity == "error"]
    if not errors:
        return ReconciliationExitCode.EXACT
    if any(issue.category == "source_not_ready" for issue in errors):
        return ReconciliationExitCode.SOURCE_NOT_READY
    if any(issue.category in {"source", "quantity"} for issue in errors):
        return ReconciliationExitCode.DATA_MISMATCH
    if any(issue.category in {"contract", "money", "determinism"} for issue in errors):
        return ReconciliationExitCode.MONEY_OR_CONTRACT_MISMATCH
    if all(issue.category == "coverage" for issue in errors):
        return ReconciliationExitCode.COVERAGE_GAP
    return ReconciliationExitCode.INTERNAL_ERROR


def run_reconciliation(
    as_of: str,
    history_days: int,
    plan_factory: Callable[[], Any],
    *,
    repeat_builds: int = 2,
    strict_coverage: bool = False,
    readiness_loader: Callable[[str, int], dict[str, Any]] | None = None,
    epoch_loader: Callable[[str, int], str] | None = None,
    facts_loader: Callable[[Mapping[str, Any], str], SourceFacts] | None = None,
) -> ReconciliationReport:
    """Run a complete read-only audit around a caller-supplied plan builder."""
    rolling_coverage(as_of, history_days)
    readiness_loader = readiness_loader or repo.procurement_source_readiness
    epoch_loader = epoch_loader or collect_source_epoch
    facts_loader = facts_loader or collect_source_facts
    repeat_builds = max(1, int(repeat_builds))

    issues: list[ReconciliationIssue] = []
    source_epoch_before = epoch_loader(as_of, history_days)
    readiness = readiness_loader(as_of, history_days)
    if readiness.get("ready") is not True:
        _issue(
            issues,
            "S001",
            "source_not_ready",
            "procurement source readiness failed",
            expected=True,
            actual={
                "ready": readiness.get("ready"),
                "reason": readiness.get("reason"),
            },
        )
        source_epoch_after = epoch_loader(as_of, history_days)
        return ReconciliationReport(
            as_of=as_of,
            exit_code=exit_code_for_issues(issues),
            issues=issues,
            source_epoch_before=source_epoch_before,
            source_epoch_after=source_epoch_after,
            plan_digests=[],
            source_readiness=readiness,
            metrics={},
        )

    try:
        plans = [_as_plan_dict(plan_factory()) for _ in range(repeat_builds)]
    except (NativePurchaseCostError, masters.MastersReadError) as exc:
        _issue(
            issues,
            "S003",
            "source_not_ready",
            "required buyer-maintained input unavailable during plan build"
            if isinstance(exc, masters.MastersReadError)
            else "verified purchase-cost input unavailable during plan build",
        )
        return ReconciliationReport(
            as_of,
            exit_code_for_issues(issues),
            issues,
            source_epoch_before,
            epoch_loader(as_of, history_days),
            [],
            readiness,
            {},
        )
    stable_plans = []
    for plan in plans:
        stable = dict(plan)
        if "cost_observation_manifest" in stable:
            stable["cost_observation_manifest"] = stable_manifest(stable["cost_observation_manifest"])
        stable_plans.append(stable)
    digests = [canonical_json_digest(plan) for plan in stable_plans]
    if len(set(digests)) != 1:
        _issue(
            issues,
            "D001",
            "determinism",
            "repeated read-only builds produced different financial or plan input",
            expected=digests[0],
            actual=digests[1:],
        )

    try:
        facts = facts_loader(plans[0], as_of)
    except (NativePurchaseCostError, masters.MastersReadError) as exc:
        _issue(
            issues,
            "S003",
            "source_not_ready",
            "required buyer-maintained input unavailable"
            if isinstance(exc, masters.MastersReadError)
            else "verified purchase-cost input unavailable",
        )
        return ReconciliationReport(
            as_of,
            exit_code_for_issues(issues),
            issues,
            source_epoch_before,
            epoch_loader(as_of, history_days),
            digests,
            readiness,
            {},
        )
    if not facts.cost_manifest_matches:
        _issue(issues, "S004", "source_not_ready", "purchase-cost financial input changed since plan build")
        return ReconciliationReport(
            as_of,
            exit_code_for_issues(issues),
            issues,
            source_epoch_before,
            epoch_loader(as_of, history_days),
            digests,
            readiness,
            facts.metrics,
        )
    plan_issues, metrics = validate_canonical_plan(
        plans[0],
        facts,
        as_of,
        strict_coverage=strict_coverage,
    )
    issues.extend(plan_issues)

    source_epoch_after = epoch_loader(as_of, history_days)
    if source_epoch_before != source_epoch_after:
        _issue(
            issues,
            "S002",
            "source",
            "source epoch changed while reconciliation was running",
            expected=source_epoch_before,
            actual=source_epoch_after,
        )
    metrics["repeat_builds"] = repeat_builds
    metrics["deterministic_builds"] = len(set(digests)) == 1
    return ReconciliationReport(
        as_of=as_of,
        exit_code=exit_code_for_issues(issues),
        issues=issues,
        source_epoch_before=source_epoch_before,
        source_epoch_after=source_epoch_after,
        plan_digests=digests,
        source_readiness=readiness,
        metrics=metrics,
    )
