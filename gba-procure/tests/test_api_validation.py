"""API validation, source-readiness and cache-epoch tests without live services."""
from __future__ import annotations

from datetime import date

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.main import CartPlanRequest, ProducerProfileUpdate, app
from app.core.config import get_settings
from app.domain.models import CartReplenishmentPlan

_HEADERS = {"X-Internal-Api-Key": get_settings().internal_api_key}
client = TestClient(app)


def _source(*, ready: bool = True, reason: str | None = None) -> dict:
    return {
        "ready": ready,
        "reason": reason,
        "producer_count": 2 if ready else 0,
        "product_count": 12 if ready else 0,
        "source_fingerprint": "source-fp",
        "as_of": "2026-06-15",
    }


def _canonical_zero_payload(source_fingerprint: str = "source-fp") -> dict:
    return {
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


def test_plan_producer_malformed_date_422():
    r = client.post(
        "/plan/producer",
        headers=_HEADERS,
        json={"producer_id": 1, "as_of_date": "not-a-date"},
    )
    assert r.status_code == 422


def test_plan_producer_nonpositive_id_422():
    assert client.post(
        "/plan/producer",
        headers=_HEADERS,
        json={"producer_id": 0},
    ).status_code == 422


def test_plan_producer_fractional_id_422():
    assert client.post(
        "/plan/producer",
        headers=_HEADERS,
        json={"producer_id": 1.5},
    ).status_code == 422


def test_cart_plan_unknown_method_422():
    r = client.post("/plan/cart", headers=_HEADERS, json={"method": "banana"})
    assert r.status_code == 422


def test_cart_plan_negative_budget_422():
    assert client.post(
        "/plan/cart",
        headers=_HEADERS,
        json={"budget_eur": -5},
    ).status_code == 422


def test_cart_plan_bounds():
    CartPlanRequest(budget_eur=0, limit=0, active_days=1)
    CartPlanRequest(budget_eur=1500, limit=1000, active_days=730)
    with pytest.raises(ValidationError):
        CartPlanRequest(budget_eur=-5)
    with pytest.raises(ValidationError):
        CartPlanRequest(limit=1001)
    with pytest.raises(ValidationError):
        CartPlanRequest(active_days=0)


def test_charts_malformed_date_422():
    r = client.post(
        "/plan/charts",
        headers=_HEADERS,
        json={"as_of_date": "2026-13-99"},
    )
    assert r.status_code == 422


def test_master_profile_uses_bounded_numeric_autonomy_level():
    assert ProducerProfileUpdate(producer_id=1, autonomy_level=2).autonomy_level == 2
    with pytest.raises(ValidationError):
        ProducerProfileUpdate(producer_id=1, autonomy_level=3)
    with pytest.raises(ValidationError):
        ProducerProfileUpdate(producer_id=1, autonomy_level="manual")


@pytest.mark.parametrize(
    "path",
    [
        "/masters/producer?producer_id=0",
        "/masters/product-terms?producer_id=-1",
        "/feedback/learned?producer_id=0",
    ],
)
def test_master_and_feedback_queries_require_positive_ids(path):
    assert client.get(path, headers=_HEADERS).status_code == 422


def test_seed_terms_rejects_unbounded_sample_count():
    assert client.post(
        "/masters/seed-terms?min_orders=0",
        headers=_HEADERS,
    ).status_code == 422


def test_startup_cart_warmer_skips_matching_canonical_and_charts(monkeypatch):
    from app.api import main
    from app.services.replenishment import worker

    monkeypatch.setattr(main, "_today", lambda: "2026-06-15")
    monkeypatch.setattr(worker, "get_source_readiness", lambda *args, **kwargs: _source())

    def cached(key):
        if ":cart:" in key:
            return _canonical_zero_payload()
        return {"top_items": [], "_source_fingerprint": "source-fp"}

    monkeypatch.setattr(main.cache, "get", cached)
    calls = []
    monkeypatch.setattr(worker, "warm_cart", lambda **kwargs: calls.append(("cart", kwargs)))
    monkeypatch.setattr(worker, "warm_charts", lambda **kwargs: calls.append(("charts", kwargs)))

    main._warm_cart_on_startup()

    assert calls == []


def test_startup_cart_warmer_rebuilds_stale_source_epoch(monkeypatch):
    from app.api import main
    from app.services.replenishment import worker

    monkeypatch.setattr(main, "_today", lambda: "2026-06-15")
    monkeypatch.setattr(worker, "get_source_readiness", lambda *args, **kwargs: _source())
    monkeypatch.setattr(
        main.cache,
        "get",
        lambda key: {
            "item_count": 0,
            "items": [],
            "_source_fingerprint": "old-fp",
        },
    )
    deleted = []
    monkeypatch.setattr(main.cache, "delete", lambda key: deleted.append(key))
    calls = []
    monkeypatch.setattr(
        worker,
        "warm_cart",
        lambda **kwargs: calls.append(("cart", kwargs))
        or {"items": 0, "business_ready": True},
    )
    monkeypatch.setattr(
        worker,
        "warm_charts",
        lambda **kwargs: calls.append(("charts", kwargs))
        or {"top_items": 0},
    )

    main._warm_cart_on_startup()

    assert [name for name, _ in calls] == ["cart", "charts"]
    assert worker.cart_cache_key("2026-06-15") in deleted


class _HealthyConnection:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def exec_driver_sql(self, statement):
        assert statement == "SELECT 1"


class _HealthyEngine:
    def connect(self):
        return _HealthyConnection()


def test_health_reports_actionable_source_reason(monkeypatch):
    from app.api import main
    from app.services.replenishment import worker

    monkeypatch.setattr(main, "get_engine", lambda: _HealthyEngine())
    monkeypatch.setattr(main, "_today", lambda: "2026-06-15")
    monkeypatch.setattr(main.cache, "health", lambda: True)
    monkeypatch.setattr(
        worker,
        "get_source_readiness",
        lambda *args, **kwargs: _source(
            ready=False,
            reason="storage_roles_missing",
        ),
    )

    result = main.health()

    assert result["status"] == "degraded"
    assert result["db_connected"] is True
    assert result["redis_connected"] is True
    assert result["business_ready"] is False
    assert result["business_reason"] == "storage_roles_missing"
    assert result["source_readiness"]["ready"] is False


def test_health_is_healthy_with_matching_evaluated_zero_cart(monkeypatch):
    from app.api import main
    from app.services.replenishment import worker

    monkeypatch.setattr(main, "get_engine", lambda: _HealthyEngine())
    monkeypatch.setattr(main, "_today", lambda: "2026-06-15")
    monkeypatch.setattr(main.cache, "health", lambda: True)
    monkeypatch.setattr(worker, "get_source_readiness", lambda *args, **kwargs: _source())
    monkeypatch.setattr(main.cache, "get_cart_not_ready", lambda as_of: None)
    monkeypatch.setattr(
        main.cache,
        "get",
        lambda key: _canonical_zero_payload(),
    )

    result = main.health()

    assert result["status"] == "healthy"
    assert result["business_ready"] is True
    assert result["business_reason"] is None
    assert result["canonical_cart_items"] == 0
    assert result["source_history_start"] == "2025-01-01"
    assert result["source_history_contract_ready"] is True
    assert (
        result["source_readiness"]["source_history_start"]
        == result["source_history_start"]
    )

    response = client.get("/ready", headers=_HEADERS)
    assert response.status_code == 200
    assert response.json()["status"] == "ready"


def test_current_cart_returns_503_before_build_when_source_is_not_ready(monkeypatch):
    from app.api import main
    from app.services.replenishment import worker

    monkeypatch.setattr(main, "_today", lambda: "2026-06-15")
    monkeypatch.setattr(
        worker,
        "get_source_readiness",
        lambda *args, **kwargs: _source(
            ready=False,
            reason="storage_roles_missing",
        ),
    )
    monkeypatch.setattr(main.cache, "delete", lambda key: False)
    monkeypatch.setattr(main.cache, "mark_cart_not_ready", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        main.policy,
        "build_cart_plan",
        lambda *args, **kwargs: pytest.fail("policy must not run"),
    )

    with pytest.raises(HTTPException) as exc:
        main.plan_cart(CartPlanRequest())

    assert exc.value.status_code == 503
    assert exc.value.detail == "cart_business_data_not_ready"


def test_current_evaluated_zero_cart_is_cached_as_a_valid_plan(monkeypatch):
    from app.api import main
    from app.services.replenishment import worker

    empty_plan = CartReplenishmentPlan(
        items=[],
        item_count=0,
        as_of_date="2026-06-15",
    )
    monkeypatch.setattr(main, "_today", lambda: "2026-06-15")
    monkeypatch.setattr(worker, "get_source_readiness", lambda *args, **kwargs: _source())
    monkeypatch.setattr(main.cache, "get", lambda key: None)
    monkeypatch.setattr(main.policy, "build_cart_plan", lambda *args, **kwargs: empty_plan)
    monkeypatch.setattr(main.cache, "clear_cart_not_ready", lambda as_of: True)
    writes = []
    monkeypatch.setattr(
        main.cache,
        "set",
        lambda key, value, ttl=None: writes.append((key, value, ttl)),
    )

    result = main.plan_cart(CartPlanRequest())

    assert result.item_count == 0
    assert len(writes) == 1
    assert writes[0][1]["_source_fingerprint"] == "source-fp"
    assert writes[0][2] == 691200


def test_user_filtered_historical_empty_cart_remains_valid(monkeypatch):
    from app.api import main
    from app.services.replenishment import worker

    empty_plan = CartReplenishmentPlan(
        items=[],
        item_count=0,
        as_of_date="2026-05-01",
    )
    monkeypatch.setattr(main.cache, "get", lambda key: None)
    monkeypatch.setattr(main.policy, "build_cart_plan", lambda *args, **kwargs: empty_plan)
    monkeypatch.setattr(
        worker,
        "require_source_readiness",
        lambda *args, **kwargs: pytest.fail("historical request must not use current readiness"),
    )
    writes = []
    monkeypatch.setattr(
        main.cache,
        "set",
        lambda key, value, ttl=None: writes.append((key, value, ttl)),
    )

    result = main.plan_cart(
        CartPlanRequest(
            as_of_date=date(2026, 5, 1),
            limit=0,
        )
    )

    assert result.item_count == 0
    assert len(writes) == 1
    assert writes[0][2] == 3600
    assert ":cartbudget:" in writes[0][0]
