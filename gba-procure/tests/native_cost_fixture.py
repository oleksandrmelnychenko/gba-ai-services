"""Synthetic wire proof fixtures for offline policy/cohort checks; never a live quote."""

from __future__ import annotations

import json
from datetime import date, timedelta

from app.data.native_purchase_costs import PurchaseCostRequest, parse_purchase_cost_response
from app.data.purchase_cost_cohort import PurchaseCostCohort


def make_cohort(products, as_of="2026-09-08", costs=None, units=None):
    products = tuple(sorted(set(products)))
    costs = costs or {}
    units = units or {}
    day = date.fromisoformat(as_of)
    snapshots = []
    identity = 0
    for offset in range(0, len(products), 1000):
        ids = products[offset : offset + 1000]
        req = PurchaseCostRequest(ids, None, day, 540)
        rows = []
        for (supplier, product), values in sorted(costs.items()):
            if product not in ids:
                continue
            for value in values:
                identity += 1
                known = value is not None
                rows.append(
                    {
                        "observation_id": identity,
                        "product_id": product,
                        "supplier_id": supplier,
                        "supplier_client_agreement_id": supplier * 1000 + 1,
                        "document_id": identity + 10000,
                        "document_date_utc": f"{day - timedelta(days=1)}T12:00:00Z" if known else None,
                        "unit_id": units.get(product, 1),
                        "unit_net_cost_eur": str(value) if known else None,
                        "status": "known" if known else "unknown",
                        "reason_codes": [] if known else ["financial_multiset_mismatch"],
                        "evidence_receipt_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                        "evidence_line_ordinals": [identity - 1] if known else [],
                        "financial_fingerprint": "c" * 64,
                    }
                )
        payload = {
            **req.payload(),
            "scope": "CurrentPostedReceipts",
            "resolver_version": "current-posted-receipt-cost-v1",
            "currency": "EUR",
            "observation_started_at_utc": "2026-09-08T02:10:00.0000001Z",
            "observation_completed_at_utc": "2026-09-08T02:10:01.0000002Z",
            "fingerprint": f"{offset + 1:064x}",
            "publications": [
                {
                    "for_amg": False,
                    "receipt_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                    "committed_at_utc": "2026-09-05T11:00:00Z",
                    "contract_version": "GBA-ONEC-INBOUND-V7",
                    "canonical_chain_sha256": "b" * 64,
                    "validated_at_utc": "2026-09-08T02:10:01.0000002Z",
                }
            ],
            "observations": rows,
        }
        snapshots.append(parse_purchase_cost_response(json.dumps(payload).encode(), req))
    return PurchaseCostCohort(products, day, tuple(snapshots))


def prepare_plan_stubs(monkeypatch, plans, as_of):
    """Attach actual synthetic receipt aggregates to existing plan-shaping fixtures."""
    from decimal import Decimal

    from app.services.replenishment import policy

    products = {supplier: [item.product_id for item in plan.items] for supplier, plan in plans.items()}
    costs = {}
    for supplier, plan in plans.items():
        for item in plan.items:
            if item.unit_cost_eur is None and item.line_cost_eur is not None and item.suggested_qty > 0:
                item.unit_cost_eur = float(
                    Decimal(str(item.line_cost_eur)) / Decimal(str(item.suggested_qty))
                )
            costs[supplier, item.product_id] = (
                [str(item.unit_cost_eur)] if item.unit_cost_eur is not None else [None]
            )
    cohort = make_cohort(sorted({pid for ids in products.values() for pid in ids}), as_of=as_of, costs=costs)
    for supplier, plan in plans.items():
        for item in plan.items:
            item.cost_provenance = cohort.provenance(item.product_id, supplier, item.suggested_qty)
            item.unit_margin_eur = item.value_density = None
            item.budget_priority_weight = (
                policy._URGENCY_WEIGHT[item.urgency] if item.suggested_qty > 0 else 0
            )
    monkeypatch.setattr(policy.repo, "products_for_producer", lambda pid, *args: products[pid])
    monkeypatch.setattr(policy, "read_purchase_cost_cohort", lambda *args: cohort)
    return cohort, products


def attach_cost_metadata(payload):
    """Extend legacy arithmetic fixtures with explicit synthetic captured-input metadata."""
    from app.services.replenishment import policy

    rows = payload.get("items", [])
    ids = sorted({row["product_id"] for row in rows})
    prices = {
        (row["producer_id"], row["product_id"]): [str(row["unit_cost_eur"])]
        if row.get("unit_cost_eur") is not None
        else [None]
        for row in rows
    }
    cohort = make_cohort(ids, as_of=payload["as_of_date"], costs=prices)
    payload.update(
        history_scope="demand",
        history_not_applicable=["inventory", "reservations", "purchase_costs"],
        cost_observation_manifest=cohort.manifest().model_dump(mode="json"),
        cost_total_basis="net_goods_excluding_vat_delivery_customs",
    )
    for row in rows:
        row.setdefault("urgency", "critical")
        row.setdefault("unit_sale_eur", None)
        row.update(
            cost_provenance=cohort.provenance(
                row["product_id"], row["producer_id"], row["suggested_qty"]
            ).model_dump(mode="json"),
            unit_margin_eur=None,
            value_density=None,
            sale_price_basis="unavailable"
            if row["unit_sale_eur"] is None
            else "historical_recorded_tax_basis_unverified",
            margin_unavailable_reason="sale_tax_basis_unverified",
            service_level_basis="abc_fallback_with_optional_producer_floor",
            budget_priority_weight=policy._URGENCY_WEIGHT[row["urgency"]] if row["suggested_qty"] > 0 else 0,
        )
    payload["cost_totals_certified"] = all(row["cost_provenance"]["budget_eligible"] for row in rows)
    return payload
