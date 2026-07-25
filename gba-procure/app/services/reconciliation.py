"""Read-only, executable reconciliation for the canonical procurement cart.

The expected side is intentionally calculated from independent SQL instead of calling
the procurement repositories whose output is being checked. Buyer-approved unit-cost
overrides are read independently from Mongo and overlaid with the same precedence as
the policy. The module never writes to SQL Server, Mongo or Redis; callers supply a
plan factory (or an already loaded JSON plan).
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from enum import IntEnum
from statistics import median
from typing import Any

from app.core.config import get_settings
from app.core.history import history_start_iso, rolling_coverage
from app.data import masters
from app.data import supply_repository as repo
from app.data.db import in_clause, query
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
            "schema_version": 1,
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


def _collect_cost_rows(
    product_ids: list[int],
    as_of: str,
) -> dict[tuple[int, int], list[Decimal]]:
    costs: dict[tuple[int, int], list[Decimal]] = defaultdict(list)
    syn = synthetic_product_id()
    for start in range(0, len(product_ids), _INVENTORY_CHUNK):
        chunk = product_ids[start : start + _INVENTORY_CHUNK]
        placeholders, params = in_clause("p", chunk)
        rows = query(
            f"""
            SELECT sioi.ProductID AS product_id,
                   so.ClientID AS producer_id,
                   dbo.GetExchangedToEuroValue(
                       sioi.UnitPrice, ISNULL(a.CurrencyID, 2), si.DateFrom
                   ) AS cost_eur
            FROM dbo.SupplyOrder so
            JOIN dbo.SupplyInvoice si
              ON si.SupplyOrderID = so.ID AND si.Deleted = 0
            JOIN dbo.SupplyInvoiceOrderItem sioi
              ON sioi.SupplyInvoiceID = si.ID AND sioi.Deleted = 0
            LEFT JOIN dbo.ClientAgreement ca ON ca.ID = so.ClientAgreementID
            LEFT JOIN dbo.Agreement a ON a.ID = ca.AgreementID
            WHERE so.ClientID IS NOT NULL
                  AND sioi.UnitPrice > 0
                  AND sioi.ProductID <> :syn
                  AND sioi.ProductID IN {placeholders}
                  AND si.DateFrom >= DATEADD(day, -:days, :asof)
                  AND si.DateFrom >= :history_start
                  AND si.DateFrom < :asof

            UNION ALL

            SELECT soui.ProductID AS product_id,
                   COALESCE(soui.SupplierID, sou.SupplierID) AS producer_id,
                   dbo.GetExchangedToEuroValue(
                       soui.UnitPrice, ISNULL(a.CurrencyID, 2), sou.FromDate
                   ) AS cost_eur
            FROM dbo.SupplyOrderUkraine sou
            JOIN dbo.SupplyOrderUkraineItem soui
              ON soui.SupplyOrderUkraineID = sou.ID AND soui.Deleted = 0
            LEFT JOIN dbo.ClientAgreement ca ON ca.ID = sou.ClientAgreementID
            LEFT JOIN dbo.Agreement a ON a.ID = ca.AgreementID
            WHERE sou.Deleted = 0
                  AND NOT (sou.IsFromCockpit = 1 AND sou.IsPlaced = 0)
                  AND COALESCE(soui.SupplierID, sou.SupplierID) IS NOT NULL
                  AND soui.UnitPrice > 0
                  AND soui.ProductID <> :syn
                  AND soui.ProductID IN {placeholders}
                  AND sou.FromDate >= DATEADD(day, -:days, :asof)
                  AND sou.FromDate >= :history_start
                  AND sou.FromDate < :asof
            """,
            {
                "asof": as_of,
                "days": _COST_HISTORY_DAYS,
                "history_start": history_start_iso(),
                "syn": syn,
                **params,
            },
        )
        for row in rows:
            cost = row["cost_eur"]
            if cost is None or _decimal(cost) <= 0:
                continue
            key = (int(row["producer_id"]), int(row["product_id"]))
            costs[key].append(_decimal(cost))
    return dict(costs)


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
        terms_by_product = masters.product_terms_for(producer_id, product_ids)
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
    cost_rows = _collect_cost_rows(product_ids, as_of)
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
        actual_decimal = _decimal(actual)
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


def _exact_median(values: list[Decimal]) -> Decimal:
    return _decimal(median(sorted(values))).quantize(
        _FOUR_DECIMALS,
        rounding=ROUND_HALF_UP,
    )


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
    actual_history = {
        key: payload.get(key)
        for key in expected_history
    }
    if actual_history != expected_history:
        _issue(
            issues,
            "C011",
            "contract",
            "plan history coverage metadata must match the source floor",
            expected=expected_history,
            actual=actual_history,
        )
    if payload.get("history_not_applicable") != ["inventory", "reservations"]:
        _issue(
            issues,
            "C012",
            "contract",
            "current inventory and reservations must be explicit history N/A inputs",
            expected=["inventory", "reservations"],
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
    total_qty = Decimal()
    priced_total = Decimal()
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
        total_qty += suggested_qty

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
            if expected_unit_cost is None:
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
            else:
                applied_unit_cost_overrides += 1
                expected_cost_source = "buyer unit_cost_override"

        if expected_unit_cost is None and source_costs:
            expected_unit_cost = _exact_median(source_costs)
        if expected_unit_cost is None:
            computed_unpriced += 1
            _issue(
                issues,
                "M001",
                "money",
                "needed item has no valid buyer override or factual supplier cost",
                key=key,
                actual={"unit_cost_eur": unit_cost, "line_cost_eur": line_cost},
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
        expected_line_cost = (expected_unit_cost * suggested_qty).quantize(
            _CENT,
            rounding=ROUND_HALF_UP,
        )
        _compare_decimal(
            issues,
            code="M003",
            category="money",
            message="line_cost_eur differs from exact unit cost × quantity",
            key=key,
            expected=expected_line_cost,
            actual=line_cost,
        )
        priced_total += expected_line_cost

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

    expected_total_qty = total_qty.quantize(_CENT, rounding=ROUND_HALF_UP)
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

    metrics = {
        **facts.metrics,
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

    plans = [_as_plan_dict(plan_factory()) for _ in range(repeat_builds)]
    digests = [canonical_json_digest(plan) for plan in plans]
    if len(set(digests)) != 1:
        _issue(
            issues,
            "D001",
            "determinism",
            "repeated read-only builds produced different canonical JSON",
            expected=digests[0],
            actual=digests[1:],
        )

    facts = facts_loader(plans[0], as_of)
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
