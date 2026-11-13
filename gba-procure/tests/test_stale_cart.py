"""A changed source serves the last reconciled cart with an explicit staleness stamp."""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from test_api_validation import _canonical_zero_payload, _source

from app.api import main
from app.api.main import CartPlanRequest
from app.services.replenishment import worker

AS_OF = "2026-06-15"


@pytest.fixture
def state(monkeypatch):
    store: dict[str, dict] = {}
    jobs: dict[str, object] = {}
    monkeypatch.setattr(main, "_today", lambda: AS_OF)
    monkeypatch.setattr(worker, "get_source_readiness", lambda *a, **k: _source())
    monkeypatch.setattr(main.cache, "get", store.get)
    monkeypatch.setattr(main.cache, "set", lambda key, value, ttl=None: store.__setitem__(key, value) or True)
    monkeypatch.setattr(main.cache, "delete", lambda key: store.pop(key, None) is not None)
    monkeypatch.setattr(
        main, "REQUEST_WARMS", SimpleNamespace(submit=lambda key, work: jobs.setdefault(key, work))
    )
    monkeypatch.setattr(worker, "utc_now_iso", lambda: "2026-06-15T09:30:00Z")
    return store, jobs


def _old_cart(built_at="2026-06-15T06:00:00Z"):
    payload = _canonical_zero_payload("old-fp")
    payload.update({"stale": False, "built_at": built_at, "source_changed_at": None})
    return payload


def test_never_built_cart_still_answers_503_and_queues_a_build(state):
    store, jobs = state
    with pytest.raises(HTTPException) as error:
        main.plan_cart(CartPlanRequest())
    assert error.value.status_code == 503
    assert list(jobs) == [worker.cart_cache_key(AS_OF)]


def test_changed_source_serves_last_cart_as_stale_and_queues_rebuild(state):
    store, jobs = state
    store[worker.cart_cache_key(AS_OF)] = _old_cart()
    result = main.plan_cart(CartPlanRequest())
    assert result.stale is True
    assert result.built_at == "2026-06-15T06:00:00Z"
    assert result.source_changed_at == "2026-06-15T09:30:00Z"
    assert list(jobs) == [worker.cart_cache_key(AS_OF)]
    assert store[worker.cart_cache_key(AS_OF)]["stale"] is False


def test_source_changed_at_is_the_first_notice_not_the_latest_request(state, monkeypatch):
    store, _ = state
    store[worker.cart_cache_key(AS_OF)] = _old_cart()
    main.plan_cart(CartPlanRequest())
    monkeypatch.setattr(worker, "utc_now_iso", lambda: "2026-06-15T09:45:00Z")
    assert main.plan_cart(CartPlanRequest()).source_changed_at == "2026-06-15T09:30:00Z"
    store[worker.cart_cache_key(AS_OF)] = _old_cart("2026-06-15T09:40:00Z")
    assert main.plan_cart(CartPlanRequest()).source_changed_at == "2026-06-15T09:45:00Z"


def test_last_good_copy_survives_invalidation_of_the_canonical_key(state):
    store, _ = state
    store[worker.last_good_cart_key(AS_OF)] = _old_cart()
    result = main.plan_cart(CartPlanRequest())
    assert result.stale is True and result.item_count == 0


def test_fresh_cart_is_not_stamped_stale(state):
    store, jobs = state
    fresh = _old_cart()
    fresh["_source_fingerprint"] = "source-fp"
    store[worker.cart_cache_key(AS_OF)] = fresh
    result = main.plan_cart(CartPlanRequest())
    assert result.stale is False and result.source_changed_at is None
    assert result.built_at == "2026-06-15T06:00:00Z"
    assert jobs == {}


def test_stale_variant_request_is_stamped_and_not_cached(state):
    store, _ = state
    store[worker.cart_cache_key(AS_OF)] = _old_cart()
    result = main.plan_cart(CartPlanRequest(limit=10))
    assert result.stale is True
    assert not any(":cartbudget:" in key for key in store)


def test_structurally_broken_cart_is_never_served_stale(state):
    store, _ = state
    broken = _old_cart()
    broken["item_count"] = 3
    store[worker.cart_cache_key(AS_OF)] = broken
    with pytest.raises(HTTPException) as error:
        main.plan_cart(CartPlanRequest())
    assert error.value.status_code == 503


def test_source_not_ready_keeps_503(state, monkeypatch):
    store, _ = state
    store[worker.last_good_cart_key(AS_OF)] = _old_cart()
    monkeypatch.setattr(
        worker, "get_source_readiness", lambda *a, **k: _source(ready=False, reason="no_recent_demand")
    )
    monkeypatch.setattr(main.cache, "mark_cart_not_ready", lambda *a, **k: None)
    with pytest.raises(HTTPException) as error:
        main.plan_cart(CartPlanRequest())
    assert error.value.status_code == 503
