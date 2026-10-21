"""Pure consistency checks for captured plans; no cache is a fresh source proof."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from datetime import date, timedelta
from decimal import Decimal
from fractions import Fraction

from app.data.native_purchase_costs import PurchaseCostRequest, parse_purchase_cost_response
from app.data.purchase_cost_cohort import exact_line_amount
from app.domain.models import CostObservationComponent, CostObservationManifest, CostProvenance

_RESOLVER = "current-posted-receipt-cost-v1"
_BASIS = "net_goods_excluding_vat_delivery_customs"
_WEIGHTS = {"critical": 1.0, "high": 0.7, "normal": 0.4, "none": 0.1}


def _require(value: bool) -> None:
    if not value:
        raise ValueError("captured_purchase_cost_metadata_invalid")


def validated_manifest(raw: dict, as_of: str) -> CostObservationManifest:
    _require(type(raw) is dict and set(raw) == set(CostObservationManifest.model_fields))
    manifest = CostObservationManifest.model_validate(raw, strict=True)
    _require(manifest.resolver_version == _RESOLVER and len(manifest.components) <= 100)
    seen = set()
    publications = {}
    for original, component in zip(raw["components"], manifest.components, strict=True):
        _require(type(original) is dict and set(original) == set(CostObservationComponent.model_fields))
        _require(
            component.as_of_exclusive == as_of
            and component.supplier_id is None
            and component.history_days == 540
            and not seen.intersection(component.product_ids)
        )
        request = PurchaseCostRequest(tuple(component.product_ids), None, date.fromisoformat(as_of), 540)
        expected_start = max(date(2025, 1, 1), request.as_of_exclusive - timedelta(days=540)).isoformat()
        _require(component.effective_start_date == expected_start)
        response = {
            **request.payload(),
            "scope": manifest.scope,
            "resolver_version": manifest.resolver_version,
            "currency": "EUR",
            "fingerprint": component.fingerprint,
            "observation_started_at_utc": component.observation_started_at_utc,
            "observation_completed_at_utc": component.observation_completed_at_utc,
            "publications": original["publications"],
            "observations": [],
        }
        # Reuse strict wire shape/UTC/publication validation, not any financial aggregation.
        snapshot = parse_purchase_cost_response(json.dumps(response).encode(), request)
        expected_id = hashlib.sha256(
            json.dumps(
                {"request": request.payload(), "fingerprint": snapshot.fingerprint},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        _require(component.component_id == expected_id)
        seen.update(component.product_ids)
        for publication in snapshot.publications:
            value = (
                publication.for_amg,
                publication.committed_at_utc,
                publication.contract_version,
                publication.canonical_chain_sha256,
            )
            _require(publications.get(publication.receipt_id, value) == value)
            publications[publication.receipt_id] = value
    relationship = (
        "empty_scope"
        if not manifest.components
        else ("single_snapshot" if len(manifest.components) == 1 else "separate_snapshots")
    )
    _require(manifest.snapshot_relationship == relationship)
    return manifest


def validated_provenance(
    raw: dict, product: int, qty, unit_scalar, manifest: CostObservationManifest, *, line_scalar=...
) -> CostProvenance:
    _require(type(raw) is dict and set(raw) == set(CostProvenance.model_fields))
    proof = CostProvenance.model_validate(raw, strict=True)
    quantity = Decimal(str(qty))
    _require(quantity.is_finite() and quantity >= 0)
    for values in (proof.current_unit_ids, proof.supplier_client_agreement_ids):
        _require(
            values == sorted(set(values))
            and all(type(value) is int and 0 < value < 2**63 for value in values)
        )
    counts = (proof.observation_count, proof.known_count, proof.unknown_count)
    _require(
        all(type(value) is int and 0 <= value <= 50_000 for value in counts)
        and counts[0] == counts[1] + counts[2]
    )
    reasons = {item.reason: item.count for item in proof.reason_counts}
    _require(
        len(reasons) == len(proof.reason_counts)
        and all(0 < count <= proof.unknown_count for count in reasons.values())
    )
    canonical = proof.canonical_unit_cost_eur
    value = None
    if canonical is not None:
        _require(re.fullmatch(r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?", canonical) is not None)
        value = Decimal(canonical)
        _require(value.is_finite() and value >= 0)
    usable = value is not None and math.isfinite(float(value)) and Decimal(str(float(value))) == value
    _require((unit_scalar is None) == (not usable))
    if usable:
        _require(type(unit_scalar) in (int, float) and Decimal(str(unit_scalar)) == value)
    line_value = exact_line_amount(value, quantity)[1] if usable else None
    if line_scalar is not ...:
        _require((line_scalar is None) == (line_value is None))
        if line_value is not None:
            _require(
                type(line_scalar) in (int, float) and Decimal(str(line_scalar)) == Decimal(str(line_value))
            )
    if proof.source == "buyer_supplied":
        _require(
            proof.scope == "BuyerSupplied"
            and proof.resolver_version is None
            and proof.basis == "buyer_tax_basis_unverified"
            and proof.statistic == "buyer_override"
            and proof.coverage == "buyer_supplied"
            and not proof.component_refs
            and not proof.current_unit_ids
            and not proof.supplier_client_agreement_ids
            and not any(counts)
            and not reasons
            and proof.aggregation_reason is None
            and usable
            and value > 0
            and not proof.budget_eligible
            and proof.budget_exclusion_reasons
            == ["buyer_tax_basis_unverified"] + (["no_positive_suggested_quantity"] if quantity <= 0 else [])
        )
        return proof
    _require(
        proof.scope == "CurrentPostedReceipts"
        and proof.resolver_version == _RESOLVER
        and proof.basis == _BASIS
        and proof.statistic == "observation_median_half_up_4"
    )
    components = [item for item in manifest.components if product in item.product_ids]
    _require(len(components) == 1 and proof.component_refs == [components[0].component_id])
    coverage = (
        "no_observations"
        if not counts[0]
        else ("complete" if counts[1] and not counts[2] else "partial" if counts[1] else "unknown")
    )
    _require(proof.coverage == coverage and bool(reasons) == bool(counts[2]))
    _require(
        bool(proof.current_unit_ids) == bool(counts[1])
        and bool(proof.supplier_client_agreement_ids) == bool(counts[1])
    )
    _require(
        len(proof.current_unit_ids) <= counts[1] and len(proof.supplier_client_agreement_ids) <= counts[1]
    )
    if counts[1]:
        _require(bool(components[0].publications))
    aggregation = (
        "mixed_current_units"
        if len(proof.current_unit_ids) > 1
        else ("cost_float_precision_unsupported" if value is not None and not usable else None)
    )
    _require(proof.aggregation_reason == aggregation)
    _require((value is not None) == (counts[1] > 0 and len(proof.current_unit_ids) == 1))
    if value is not None:
        _require(re.fullmatch(r"(?:0|[1-9][0-9]*)\.[0-9]{4}", canonical) is not None)
    exclusions = []
    if quantity <= 0:
        exclusions.append("no_positive_suggested_quantity")
    if coverage != "complete":
        exclusions.append(
            "no_observations" if coverage == "no_observations" else "incomplete_observation_coverage"
        )
    if aggregation:
        exclusions.append(aggregation)
    if not usable:
        exclusions.append("verified_cost_unavailable")
    elif line_value is None:
        exclusions.append("line_amount_precision_unsupported")
    _require(proof.budget_exclusion_reasons == exclusions and proof.budget_eligible == (not exclusions))
    return proof


def validate_item_metadata(item: dict, manifest: CostObservationManifest) -> None:
    required = {
        "sale_price_basis",
        "margin_unavailable_reason",
        "service_level_basis",
        "budget_priority_weight",
        "unit_margin_eur",
        "value_density",
        "cost_provenance",
        "unit_cost_eur",
        "line_cost_eur",
    }
    _require(required.issubset(item) and item["unit_margin_eur"] is None and item["value_density"] is None)
    _require(
        item["sale_price_basis"]
        == (
            "unavailable" if item.get("unit_sale_eur") is None else "historical_recorded_tax_basis_unverified"
        )
    )
    _require(
        item["margin_unavailable_reason"] == "sale_tax_basis_unverified"
        and item["service_level_basis"] == "abc_fallback_with_optional_producer_floor"
    )
    quantity = Decimal(str(item["suggested_qty"]))
    weight = _WEIGHTS[item["urgency"]] if quantity > 0 else 0.0
    _require(item["budget_priority_weight"] == weight)
    proof = validated_provenance(
        item["cost_provenance"],
        item["product_id"],
        quantity,
        item["unit_cost_eur"],
        manifest,
        line_scalar=item["line_cost_eur"],
    )
    _require(item.get("within_budget") is not True or proof.budget_eligible)
    if item.get("cheaper_alt") is not None:
        alt = item["cheaper_alt"]
        _require(
            alt.get("comparison_basis") == "historical_net_goods"
            and alt["producer_id"] > 0
            and alt["producer_id"] != item["producer_id"]
        )
        alt_proof = validated_provenance(
            alt["cost_provenance"], item["product_id"], quantity, alt["cost_eur"], manifest
        )
        _require(
            proof.source == alt_proof.source == "receipt_history"
            and item["unit_cost_eur"] is not None
            and alt["cost_eur"] is not None
        )
        base = Fraction(Decimal(proof.canonical_unit_cost_eur))
        alternative = Fraction(Decimal(alt_proof.canonical_unit_cost_eur))
        _require(alternative < base and alternative * 50 <= base * 49)


def stable_manifest(raw: dict) -> dict:
    """Identity used after a fresh proof read; preserve all financial/publication input."""
    result = copy.deepcopy(raw)
    for component in result.get("components", []):
        component.pop("observation_started_at_utc", None)
        component.pop("observation_completed_at_utc", None)
        for publication in component.get("publications", []):
            publication.pop("validated_at_utc", None)
    return result
