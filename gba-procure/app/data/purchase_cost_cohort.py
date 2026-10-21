"""One build's bounded, reusable verified observations; no persistent proof cache."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict
from datetime import date, timedelta
from decimal import Decimal
from fractions import Fraction

from app.core.config import get_settings
from app.data.native_purchase_costs import (
    NativePurchaseCostClient,
    NativePurchaseCostError,
    PurchaseCostRequest,
    PurchaseCostSnapshot,
    VerifiedCostAggregate,
    aggregate_verified_costs,
)
from app.domain.models import CostObservationComponent, CostObservationManifest, CostProvenance

HISTORY_DAYS = 540
NET_GOODS_BASIS = "net_goods_excluding_vat_delivery_customs"


def exact_line_amount(unit_price: Decimal, quantity: float | Decimal) -> tuple[Decimal, float | None]:
    """Exact extension, HALF_UP cents once, then a canonical JSON float proof."""
    qty = quantity if isinstance(quantity, Decimal) else Decimal(str(quantity))
    if not qty.is_finite() or qty < 0 or not unit_price.is_finite() or unit_price < 0:
        raise NativePurchaseCostError("invalid_purchase_cost_line_quantity")
    scaled = Fraction(unit_price) * Fraction(qty) * 100
    whole, remainder = divmod(scaled.numerator, scaled.denominator)
    cents = whole + (2 * remainder >= scaled.denominator)
    exact = Decimal(f"{cents // 100}.{cents % 100:02d}")
    value = float(exact)
    return exact, value if math.isfinite(value) and Decimal(str(value)) == exact else None


def buyer_provenance(cost: float, quantity: float | Decimal = 1) -> CostProvenance:
    """Accepted buyer input is visible but inherits no receipt or tax proof."""
    return CostProvenance(
        source="buyer_supplied",
        scope="BuyerSupplied",
        resolver_version=None,
        basis="buyer_tax_basis_unverified",
        statistic="buyer_override",
        canonical_unit_cost_eur=format(Decimal(str(cost)), "f"),
        coverage="buyer_supplied",
        budget_exclusion_reasons=["buyer_tax_basis_unverified"]
        + (["no_positive_suggested_quantity"] if Decimal(str(quantity)) <= 0 else []),
    )


class PurchaseCostCohort:
    """Explicit per-build dependency shared by producer, alternatives and cart paths.

    Products occur in exactly one bounded request. Components retain the full
    actual query scope, even in a producer subset's manifest. A chunk collection
    never claims a common transaction. Raw observations remain available for an
    independent reconciliation implementation.
    """

    def __init__(
        self,
        product_ids: tuple[int, ...],
        as_of: date,
        snapshots: tuple[PurchaseCostSnapshot, ...],
        history_days: int = HISTORY_DAYS,
    ):
        if len(product_ids) > 100_000 or len(set(product_ids)) != len(product_ids):
            raise NativePurchaseCostError("invalid_purchase_cost_cohort")
        if any(type(pid) is not int or pid <= 0 or pid >= 2**63 for pid in product_ids):
            raise NativePurchaseCostError("invalid_purchase_cost_cohort")
        self.product_ids = frozenset(product_ids)
        self.as_of = as_of
        self.history_days = history_days
        self.snapshots = snapshots
        self._groups: dict[tuple[int, int | None], VerifiedCostAggregate] = {}
        self._components: dict[int, CostObservationComponent] = {}
        self._suppliers: dict[int, list[VerifiedCostAggregate]] = {}
        if not snapshots:
            if product_ids:
                raise NativePurchaseCostError("missing_purchase_cost_components")
            return
        summary = aggregate_verified_costs(snapshots)
        actual_products = set()
        for snapshot in snapshots:
            request = snapshot.request
            if (
                request.as_of_exclusive != as_of
                or request.history_days != history_days
                or request.supplier_id is not None
            ):
                raise NativePurchaseCostError("invalid_purchase_cost_cohort")
            actual_products.update(request.product_ids)
            identity = hashlib.sha256(
                json.dumps(
                    {"request": request.payload(), "fingerprint": snapshot.fingerprint},
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            component = CostObservationComponent(
                component_id=identity,
                product_ids=sorted(request.product_ids),
                supplier_id=None,
                as_of_exclusive=as_of.isoformat(),
                history_days=history_days,
                effective_start_date=max(date(2025, 1, 1), as_of - timedelta(days=history_days)).isoformat(),
                observation_started_at_utc=snapshot.observation_started_at_utc,
                observation_completed_at_utc=snapshot.observation_completed_at_utc,
                fingerprint=snapshot.fingerprint,
                publications=[asdict(item) for item in snapshot.publications],
            )
            for product in request.product_ids:
                self._components[product] = component
        if actual_products != self.product_ids:
            raise NativePurchaseCostError("invalid_purchase_cost_cohort")
        self._groups = {(group.product_id, group.supplier_id): group for group in summary.by_product_supplier}
        for group in summary.by_product_supplier:
            if group.supplier_id is not None:
                self._suppliers.setdefault(group.product_id, []).append(group)

    def require_scope(
        self, product_ids: list[int] | tuple[int, ...], as_of: str, history_days: int = HISTORY_DAYS
    ) -> None:
        if date.fromisoformat(as_of) != self.as_of or history_days != self.history_days:
            raise NativePurchaseCostError("purchase_cost_cohort_scope_mismatch")
        if not set(product_ids).issubset(self.product_ids):
            raise NativePurchaseCostError("purchase_cost_cohort_scope_mismatch")

    def group(self, product_id: int, supplier_id: int) -> VerifiedCostAggregate:
        if product_id not in self.product_ids:
            raise NativePurchaseCostError("purchase_cost_cohort_scope_mismatch")
        return self._groups.get(
            (product_id, supplier_id),
            VerifiedCostAggregate(
                product_id,
                supplier_id,
                0,
                0,
                0,
                (),
                (),
                (),
                None,
                None,
                None,
            ),
        )

    def supplier_groups(self, product_id: int) -> tuple[VerifiedCostAggregate, ...]:
        if product_id not in self.product_ids:
            raise NativePurchaseCostError("purchase_cost_cohort_scope_mismatch")
        return tuple(self._suppliers.get(product_id, []))

    def provenance(self, product_id: int, supplier_id: int, quantity: float | Decimal) -> CostProvenance:
        group = self.group(product_id, supplier_id)
        reasons = []
        if Decimal(str(quantity)) <= 0:
            reasons.append("no_positive_suggested_quantity")
        if group.coverage != "complete":
            reasons.append(
                "no_observations"
                if group.coverage == "no_observations"
                else "incomplete_observation_coverage"
            )
        if group.aggregation_reason:
            reasons.append(group.aggregation_reason)
        if group.median_float is None:
            reasons.append("verified_cost_unavailable")
        elif exact_line_amount(group.median_unit_net_cost_eur, quantity)[1] is None:
            reasons.append("line_amount_precision_unsupported")
        return CostProvenance(
            canonical_unit_cost_eur=(
                format(group.median_unit_net_cost_eur, "f")
                if group.median_unit_net_cost_eur is not None
                else None
            ),
            current_unit_ids=list(group.unit_ids),
            supplier_client_agreement_ids=list(group.supplier_client_agreement_ids),
            observation_count=group.observation_count,
            known_count=group.known_count,
            unknown_count=group.unknown_count,
            coverage=group.coverage,
            reason_counts=[{"reason": reason, "count": count} for reason, count in group.reason_counts],
            aggregation_reason=group.aggregation_reason,
            component_refs=[self._components[product_id].component_id],
            budget_eligible=not reasons,
            budget_exclusion_reasons=reasons,
        )

    def manifest(self, product_ids: list[int] | tuple[int, ...] | None = None) -> CostObservationManifest:
        products = self.product_ids if product_ids is None else set(product_ids)
        if not products.issubset(self.product_ids):
            raise NativePurchaseCostError("purchase_cost_cohort_scope_mismatch")
        components = {self._components[pid].component_id: self._components[pid] for pid in products}
        return CostObservationManifest(
            snapshot_relationship=(
                "single_snapshot"
                if len(components) == 1
                else "separate_snapshots"
                if components
                else "empty_scope"
            ),
            components=[components[key] for key in sorted(components)],
        )

    def reread_unchanged(self, client: NativePurchaseCostClient | None = None) -> bool:
        """Fresh exact scopes; no broad watermark or cached proof substitution."""
        if not self.snapshots:
            return True
        reader = client or NativePurchaseCostClient.from_settings(get_settings())
        return all(
            reader.read(snapshot.request).fingerprint == snapshot.fingerprint for snapshot in self.snapshots
        )


def read_purchase_cost_cohort(
    product_ids: list[int] | tuple[int, ...],
    as_of: str,
    history_days: int = HISTORY_DAYS,
    client: NativePurchaseCostClient | None = None,
) -> PurchaseCostCohort:
    if any(type(pid) is not int or not 0 < pid < 2**63 for pid in product_ids) or len(product_ids) > 100_000:
        raise NativePurchaseCostError("invalid_purchase_cost_cohort")
    ids = tuple(sorted(set(product_ids)))
    day = date.fromisoformat(as_of)
    if not ids:
        return PurchaseCostCohort((), day, (), history_days)
    reader = client or NativePurchaseCostClient.from_settings(get_settings())
    snapshots = []
    rows = 0
    for offset in range(0, len(ids), 1000):
        snapshot = reader.read(PurchaseCostRequest(ids[offset : offset + 1000], None, day, history_days))
        rows += len(snapshot.observations)
        if rows > 50_000:
            raise NativePurchaseCostError("purchase_cost_aggregation_too_large")
        snapshots.append(snapshot)
    return PurchaseCostCohort(ids, day, tuple(snapshots), history_days)
