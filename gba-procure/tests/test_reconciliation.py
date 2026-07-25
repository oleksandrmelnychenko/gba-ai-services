"""Pure reconciliation-gate tests: product, quantity, cent and exit contracts."""

from __future__ import annotations

import copy
import json
from decimal import Decimal

from app.services.reconciliation import (
    InventoryFact,
    ReconciliationExitCode,
    ReconciliationReport,
    SourceFacts,
    canonical_json_digest,
    exit_code_for_issues,
    run_reconciliation,
    validate_canonical_plan,
)

AS_OF = "2026-07-25"


def _item(
    product_id: int,
    producer_id: int,
    qty: float,
    unit_cost: float,
    line_cost: float,
) -> dict:
    return {
        "product_id": product_id,
        "producer_id": producer_id,
        "suggested_qty": qty,
        "unit_cost_eur": unit_cost,
        "line_cost_eur": line_cost,
        "forecast": {
            "product_id": product_id,
            "mean_daily": 1.0,
            "std_daily": 0.5,
            "method": "moving_average_v0",
            "horizon_days": 30,
            "forecast_units": 30.0,
        },
        "inventory": {
            "product_id": product_id,
            "on_hand": 10.0,
            "reserved": 3.0,
            "available": 7.0,
            "on_order": 6.0,
            "position": 13.0,
        },
    }


def _payload() -> dict:
    items = [
        _item(101, 501, 50.0, 3.8083, 190.42),
        _item(102, 502, 1.0, 23.425, 23.43),
    ]
    return {
        "items": items,
        "item_count": 2,
        "total_item_count": 2,
        "is_truncated": False,
        "duplicate_supplier_options_removed": 1,
        "total_suggested_qty": 51.0,
        "priced_cost_eur": 213.85,
        "total_cost_eur": 213.85,
        "unpriced_item_count": 0,
        "as_of_date": AS_OF,
        "model_version": "test",
    }


def _facts() -> SourceFacts:
    inventory = InventoryFact(
        gross_on_hand=Decimal("10"),
        reserved=Decimal("3"),
        available=Decimal("7"),
        on_order=Decimal("6"),
    )
    availability = {
        (101, 11): Decimal("7"),
        (102, 11): Decimal("7"),
    }
    return SourceFacts(
        inventory_by_product={101: inventory, 102: inventory},
        cost_rows_by_pair={
            (501, 101): [Decimal("3.8083")],
            # Exact midpoint must round up to 23.4250, never through binary float.
            (502, 102): [Decimal("23.4249"), Decimal("23.4250")],
        },
        availability_by_key=availability,
        consignment_by_key=dict(availability),
        metrics={
            "products_checked": 2,
            "products_with_reservations": 2,
            "products_with_on_order": 2,
            "priced_selected_pairs": 2,
        },
    )


def _ready(_as_of: str, _history_days: int) -> dict:
    return {"ready": True, "reason": None, "source_fingerprint": "source"}


def test_canonical_json_digest_ignores_object_key_order_but_not_item_order():
    payload = _payload()
    reordered_keys = dict(reversed(list(payload.items())))
    assert canonical_json_digest(payload) == canonical_json_digest(reordered_keys)

    reversed_items = {**payload, "items": list(reversed(payload["items"]))}
    assert canonical_json_digest(payload) != canonical_json_digest(reversed_items)


def test_exact_plan_reconciles_nested_ids_inventory_decimal_median_and_line_cents():
    issues, metrics = validate_canonical_plan(_payload(), _facts(), AS_OF)

    assert [issue for issue in issues if issue.severity == "error"] == []
    assert metrics["plan_items"] == 2
    assert metrics["unique_plan_products"] == 2
    assert metrics["computed_total_suggested_qty"] == Decimal("51.00")
    assert metrics["computed_priced_cost_eur"] == Decimal("213.85")
    assert exit_code_for_issues(issues) == ReconciliationExitCode.EXACT


def test_contract_gate_rejects_duplicate_product_truncation_and_nested_id_drift():
    payload = _payload()
    duplicate = copy.deepcopy(payload["items"][0])
    duplicate["producer_id"] = 999
    payload["items"].append(duplicate)
    payload["item_count"] = 3
    payload["is_truncated"] = True
    payload["items"][0]["forecast"]["product_id"] = 9999

    issues, _metrics = validate_canonical_plan(payload, _facts(), AS_OF)
    codes = {issue.code for issue in issues}

    assert {"C003", "C004", "C008", "C010"}.issubset(codes)
    assert exit_code_for_issues(issues) == ReconciliationExitCode.MONEY_OR_CONTRACT_MISMATCH


def test_money_gate_rejects_a_single_cent_under_round():
    payload = _payload()
    payload["items"][0]["line_cost_eur"] = 190.41
    payload["priced_cost_eur"] = 213.84
    payload["total_cost_eur"] = 213.84

    issues, _metrics = validate_canonical_plan(payload, _facts(), AS_OF)

    assert any(issue.code == "M003" and issue.key["product_id"] == 101 for issue in issues)
    assert exit_code_for_issues(issues) == ReconciliationExitCode.MONEY_OR_CONTRACT_MISMATCH


def test_inventory_gate_reports_product_field_and_storage_drift():
    payload = _payload()
    payload["items"][0]["inventory"]["available"] = 6.5
    facts = _facts()
    facts.consignment_by_key[(101, 11)] = Decimal("6.75")

    issues, _metrics = validate_canonical_plan(payload, facts, AS_OF)

    assert any(
        issue.code == "Q004" and issue.key["product_id"] == 101 and issue.key["field"] == "available"
        for issue in issues
    )
    assert any(
        issue.code == "Q005" and issue.key == {"product_id": 101, "storage_id": 11} for issue in issues
    )
    assert exit_code_for_issues(issues) == ReconciliationExitCode.DATA_MISMATCH


def test_runner_detects_source_epoch_change():
    epochs = iter(["before", "after"])
    report = run_reconciliation(
        AS_OF,
        120,
        _payload,
        repeat_builds=1,
        readiness_loader=_ready,
        epoch_loader=lambda _as_of, _days: next(epochs),
        facts_loader=lambda _payload, _as_of: _facts(),
    )

    assert report.exit_code == ReconciliationExitCode.DATA_MISMATCH
    assert any(issue.code == "S002" for issue in report.issues)


def test_runner_detects_nondeterministic_item_sequence():
    payloads = [_payload(), _payload()]
    payloads[1]["items"] = list(reversed(payloads[1]["items"]))
    payload_iter = iter(payloads)

    report = run_reconciliation(
        AS_OF,
        120,
        lambda: next(payload_iter),
        repeat_builds=2,
        readiness_loader=_ready,
        epoch_loader=lambda _as_of, _days: "stable",
        facts_loader=lambda _payload, _as_of: _facts(),
    )

    assert report.exit_code == ReconciliationExitCode.MONEY_OR_CONTRACT_MISMATCH
    assert any(issue.code == "D001" for issue in report.issues)


def test_runner_fails_fast_when_source_is_not_ready():
    report = run_reconciliation(
        AS_OF,
        120,
        lambda: (_ for _ in ()).throw(AssertionError("plan must not build")),
        readiness_loader=lambda _as_of, _days: {
            "ready": False,
            "reason": "sellable_inventory_missing",
        },
        epoch_loader=lambda _as_of, _days: "stable",
        facts_loader=lambda _payload, _as_of: (_ for _ in ()).throw(AssertionError("facts must not load")),
    )

    assert report.exit_code == ReconciliationExitCode.SOURCE_NOT_READY
    assert report.plan_digests == []
    assert report.issues[0].code == "S001"


def test_strict_live_coverage_uses_dedicated_exit_code():
    facts = _facts()
    facts.metrics["products_with_reservations"] = 0
    facts.metrics["products_with_on_order"] = 0

    issues, _metrics = validate_canonical_plan(
        _payload(),
        facts,
        AS_OF,
        strict_coverage=True,
    )

    assert {issue.code for issue in issues if issue.severity == "error"} == {"G001", "G002"}
    assert exit_code_for_issues(issues) == ReconciliationExitCode.COVERAGE_GAP


def test_report_is_machine_serializable_with_stable_exit_contract():
    report = ReconciliationReport(
        as_of=AS_OF,
        exit_code=ReconciliationExitCode.EXACT,
        issues=[],
        source_epoch_before="epoch",
        source_epoch_after="epoch",
        plan_digests=["digest"],
        source_readiness={"ready": True},
        metrics={"money": Decimal("213.85")},
    )

    encoded = json.dumps(report.to_dict(), sort_keys=True)
    decoded = json.loads(encoded)

    assert decoded["ok"] is True
    assert decoded["exit_code"] == 0
    assert decoded["metrics"]["money"] == "213.85"
