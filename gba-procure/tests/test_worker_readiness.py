"""Input-aware business-readiness guards for procurement warm-up jobs."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.domain.models import MODEL_VERSION, CartReplenishmentPlan, PlanCharts
from app.services.replenishment import worker


def _source(*, ready: bool = True, reason: str | None = None) -> dict:
    return {
        "ready": ready,
        "reason": reason,
        "producer_count": 2 if ready else 0,
        "product_count": 12 if ready else 0,
        "source_fingerprint": "source-fp",
        "as_of": "2026-06-15",
    }


def _plan(item_count: int):
    return SimpleNamespace(
        item_count=item_count,
        model_dump=lambda mode: {"item_count": item_count, "items": []},
    )


def _canonical_zero_payload(source_fingerprint: str = "source-fp") -> dict:
    return {
        "as_of_date": "2026-06-15",
        "source_history_start": "2025-01-01",
        "effective_start": "2026-02-15",
        "effective_history_days": 120,
        "history_complete": True,
        "history_not_applicable": ["inventory", "reservations"],
        "model_version": MODEL_VERSION,
        "item_count": 0,
        "total_item_count": 0,
        "is_truncated": False,
        "duplicate_supplier_options_removed": 0,
        "total_suggested_qty": 0.0,
        "total_cost_eur": 0.0,
        "priced_cost_eur": 0.0,
        "unpriced_item_count": 0,
        "items": [],
        "_source_fingerprint": source_fingerprint,
    }


def _canonical_item_payload() -> dict:
    item = {
        "product_id": 101,
        "producer_id": 501,
        "suggested_qty": 3.0,
        # Supplier/override rates retain four decimals; booked amounts are exact cents.
        "unit_cost_eur": 3.8083,
        "line_cost_eur": 11.42,
        "forecast": {"product_id": 101},
        "inventory": {"product_id": 101},
    }
    return {
        "as_of_date": "2026-06-15",
        "source_history_start": "2025-01-01",
        "effective_start": "2026-02-15",
        "effective_history_days": 120,
        "history_complete": True,
        "history_not_applicable": ["inventory", "reservations"],
        "model_version": MODEL_VERSION,
        "item_count": 1,
        "total_item_count": 1,
        "is_truncated": False,
        "duplicate_supplier_options_removed": 0,
        "total_suggested_qty": 3.0,
        "total_cost_eur": 11.42,
        "priced_cost_eur": 11.42,
        "unpriced_item_count": 0,
        "items": [item],
        "_source_fingerprint": "source-fp",
    }


def test_warm_cart_persists_an_evaluated_zero_item_plan_when_inputs_are_ready(
    monkeypatch,
):
    empty_plan = CartReplenishmentPlan(
        items=[],
        item_count=0,
        total_item_count=0,
        total_cost_eur=0.0,
        as_of_date="2026-06-15",
    )
    monkeypatch.setattr(worker, "get_source_readiness", lambda *args, **kwargs: _source())
    monkeypatch.setattr(worker.policy, "build_cart_plan", lambda *args, **kwargs: empty_plan)
    writes = []
    stored = {}

    def persist(key, value, ttl=None):
        writes.append((key, value, ttl))
        stored[key] = value
        return True

    monkeypatch.setattr(
        worker.cache,
        "set",
        persist,
    )
    monkeypatch.setattr(worker.cache, "get", stored.get)
    monkeypatch.setattr(worker.cache, "clear_cart_not_ready", lambda as_of: True)

    result = worker.warm_cart(as_of="2026-06-15")

    assert result["business_ready"] is True
    assert result["candidates"] == 2
    assert result["items"] == 0
    assert len(writes) == 1
    assert writes[0][1]["_source_fingerprint"] == "source-fp"
    assert writes[0][2] == 691200


def test_warm_cart_rejects_incomplete_inputs_before_policy_evaluation(monkeypatch):
    monkeypatch.setattr(
        worker,
        "get_source_readiness",
        lambda *args, **kwargs: _source(
            ready=False,
            reason="sellable_storage_scope_unconfigured",
        ),
    )
    monkeypatch.setattr(
        worker.policy,
        "build_cart_plan",
        lambda *args, **kwargs: pytest.fail("policy must not run on incomplete inputs"),
    )
    monkeypatch.setattr(worker.cache, "delete", lambda key: False)
    markers = []
    monkeypatch.setattr(
        worker.cache,
        "mark_cart_not_ready",
        lambda as_of, reason, **kwargs: markers.append((as_of, reason, kwargs)),
    )

    with pytest.raises(
        worker.ProcurementBusinessReadinessError,
        match="sellable_storage_scope_unconfigured",
    ):
        worker.warm_cart(as_of="2026-06-15")

    assert markers == [
        (
            "2026-06-15",
            "sellable_storage_scope_unconfigured",
            {"candidate_count": 0, "item_count": 0},
        )
    ]


def test_source_readiness_fails_closed_when_required_masters_store_is_down(monkeypatch):
    monkeypatch.setattr(worker.cache, "get", lambda key: None)
    monkeypatch.setattr(worker.cache, "set", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "app.data.supply_repository.procurement_source_readiness",
        lambda as_of, history_days: _source(),
    )
    monkeypatch.setattr(worker.masters, "ping", lambda: False)

    snapshot = worker.get_source_readiness("2026-06-15", force=True)

    assert snapshot["ready"] is False
    assert snapshot["reason"] == "masters_store_unavailable"
    assert snapshot["masters_connected"] is False


def test_full_producer_warm_accepts_zero_suggestions_when_inputs_are_ready(monkeypatch):
    monkeypatch.setattr(worker, "get_source_readiness", lambda *args, **kwargs: _source())
    monkeypatch.setattr(worker, "warm_producer_candidates", lambda as_of: [10, 20])
    monkeypatch.setattr(worker.policy, "build_plan", lambda *args, **kwargs: _plan(0))
    writes = []
    monkeypatch.setattr(
        worker.cache,
        "set",
        lambda key, value, ttl=None: writes.append((key, value, ttl)) or True,
    )

    result = worker.run(
        as_of="2026-06-15",
        warm_cart_key=False,
    )

    assert result["business_ready"] is True
    assert result["producer_items"] == 0
    assert len(writes) == 2
    assert all(value["_source_fingerprint"] == "source-fp" for _, value, _ in writes)


def test_resume_uses_only_producer_cache_from_the_same_source_epoch(monkeypatch):
    monkeypatch.setattr(worker, "get_source_readiness", lambda *args, **kwargs: _source())
    monkeypatch.setattr(worker, "warm_producer_candidates", lambda as_of: [10, 20])
    cached = {
        worker.cache.make_key("producer", 10, "2026-06-15"): {
            "item_count": 2,
            "_source_fingerprint": "source-fp",
        },
        worker.cache.make_key("producer", 20, "2026-06-15"): {
            "item_count": 0,
            "_source_fingerprint": "source-fp",
        },
    }
    monkeypatch.setattr(worker.cache, "get", cached.get)
    monkeypatch.setattr(
        worker.policy,
        "build_plan",
        lambda *args, **kwargs: pytest.fail("matching cached plans must be skipped"),
    )

    result = worker.run(
        as_of="2026-06-15",
        warm_cart_key=False,
        skip_existing=True,
    )

    assert result["business_ready"] is True
    assert result["ok"] == 0
    assert result["skipped"] == 2
    assert result["nonempty_producers"] == 1
    assert result["producer_items"] == 2


def test_zero_item_canonical_payload_is_structurally_valid_for_ready_source():
    assert worker.canonical_cart_payload_is_ready(
        _canonical_zero_payload(),
        source_fingerprint="source-fp",
    )


def test_canonical_payload_from_an_old_source_epoch_is_rejected():
    assert not worker.canonical_cart_payload_is_ready(
        _canonical_zero_payload("old-fp"),
        source_fingerprint="source-fp",
    )


def test_canonical_payload_rejects_duplicate_unpriced_or_cent_drift():
    valid = _canonical_item_payload()
    item = valid["items"][0]
    assert worker.canonical_cart_payload_is_ready(valid, source_fingerprint="source-fp")

    duplicate = {
        **valid,
        "item_count": 2,
        "total_item_count": 2,
        "total_suggested_qty": 6.0,
        "total_cost_eur": 22.84,
        "priced_cost_eur": 22.84,
        "items": [item, dict(item)],
    }
    assert not worker.canonical_cart_payload_is_ready(duplicate)

    unpriced = {
        **valid,
        "total_cost_eur": None,
        "priced_cost_eur": 0.0,
        "unpriced_item_count": 1,
        "items": [{**item, "unit_cost_eur": None, "line_cost_eur": None}],
    }
    assert not worker.canonical_cart_payload_is_ready(unpriced)

    cents_drift = {**valid, "items": [{**item, "line_cost_eur": 11.41}]}
    assert not worker.canonical_cart_payload_is_ready(cents_drift)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("line_cost_eur", 11.424),
        ("priced_cost_eur", 11.424),
        ("total_cost_eur", 11.424),
    ],
)
def test_canonical_payload_rejects_subcent_booked_money(
    field: str,
    value: float,
):
    payload = _canonical_item_payload()
    if field == "line_cost_eur":
        payload["items"][0][field] = value
    else:
        payload[field] = value

    assert not worker.canonical_cart_payload_is_ready(payload)


def test_canonical_payload_rejects_zero_suggested_quantity():
    payload = _canonical_item_payload()
    payload["items"][0]["suggested_qty"] = 0.0
    payload["items"][0]["line_cost_eur"] = 0.0
    payload["total_suggested_qty"] = 0.0
    payload["priced_cost_eur"] = 0.0
    payload["total_cost_eur"] = 0.0

    assert not worker.canonical_cart_payload_is_ready(payload)


def test_warm_charts_accepts_evaluated_zero_cart_from_same_source_epoch(monkeypatch):
    monkeypatch.setattr(worker, "get_source_readiness", lambda *args, **kwargs: _source())
    monkeypatch.setattr(worker.cache, "get_cart_not_ready", lambda as_of: None)
    monkeypatch.setattr(
        worker.cache,
        "get",
        lambda key: _canonical_zero_payload(),
    )
    charts = PlanCharts(
        producer_id=None,
        as_of_date="2026-06-15",
        urgency_mix=[],
        days_of_cover_hist=[],
        top_items=[],
        demand_series=[],
    )
    monkeypatch.setattr(worker.policy, "build_charts", lambda *args, **kwargs: charts)
    writes = []
    stored = {worker.cart_cache_key("2026-06-15"): _canonical_zero_payload()}

    def get_cached(key):
        return stored.get(key)

    def persist(key, value, ttl=None):
        writes.append((key, value, ttl))
        stored[key] = value
        return True

    monkeypatch.setattr(worker.cache, "get", get_cached)
    monkeypatch.setattr(
        worker.cache,
        "set",
        persist,
    )

    result = worker.warm_charts(as_of="2026-06-15")

    assert result["top_items"] == 0
    assert writes[0][1]["_source_fingerprint"] == "source-fp"


def test_warm_cart_fails_closed_when_redis_does_not_confirm_write(monkeypatch):
    empty_plan = CartReplenishmentPlan(
        items=[],
        item_count=0,
        total_item_count=0,
        total_cost_eur=0.0,
        as_of_date="2026-06-15",
    )
    monkeypatch.setattr(worker, "get_source_readiness", lambda *args, **kwargs: _source())
    monkeypatch.setattr(worker.policy, "build_cart_plan", lambda *args, **kwargs: empty_plan)
    monkeypatch.setattr(worker.cache, "set", lambda *args, **kwargs: False)
    monkeypatch.setattr(worker.cache, "delete", lambda key: False)
    markers = []
    monkeypatch.setattr(
        worker.cache,
        "mark_cart_not_ready",
        lambda as_of, reason, **kwargs: markers.append((as_of, reason)),
    )

    with pytest.raises(
        worker.ProcurementBusinessReadinessError,
        match="canonical_cart_cache_write_failed",
    ):
        worker.warm_cart(as_of="2026-06-15")

    assert markers == [("2026-06-15", "canonical_cart_cache_write_failed")]


def test_warm_charts_refuses_stale_canonical_cart(monkeypatch):
    monkeypatch.setattr(worker, "get_source_readiness", lambda *args, **kwargs: _source())
    monkeypatch.setattr(worker.cache, "get_cart_not_ready", lambda as_of: None)
    monkeypatch.setattr(
        worker.cache,
        "get",
        lambda key: _canonical_zero_payload("old-fp"),
    )
    deleted = []
    monkeypatch.setattr(worker.cache, "delete", lambda key: deleted.append(key))
    monkeypatch.setattr(
        worker.policy,
        "build_charts",
        lambda *args, **kwargs: pytest.fail("charts must not use a stale cart"),
    )

    with pytest.raises(
        worker.ProcurementBusinessReadinessError,
        match="canonical_cart_not_ready",
    ):
        worker.warm_charts(as_of="2026-06-15")

    assert worker.cache.make_key(
        "charts",
        f"all:{worker.CHARTS_TOP_N}",
        "2026-06-15",
    ) in deleted
