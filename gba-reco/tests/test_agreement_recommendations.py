"""Agreement isolation across calculation, API, cache warming and retry behavior."""

from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api.main import app
from app.data import cache
from app.data import sales_repository as repo
from app.services import scheduler
from app.services.recommendations import copurchase, live_remap, service, worker

A = "8a375ce0-47fe-4804-b1e6-a7c8787dd880"
B = "f2d9fa15-0e65-40df-83ff-70bbc5349272"
AS_OF = "2026-09-07"


@pytest.fixture
def scoped_data(monkeypatch):
    monkeypatch.setattr(repo, "client_exists", lambda cid: True)
    monkeypatch.setattr(repo, "active_agreements", lambda cid: [A, B] if cid == 7 else [])
    monkeypatch.setattr(repo, "count_orders_before", lambda *args, **kw: 5)
    monkeypatch.setattr(repo, "ubiquitous_product_ids", lambda *args: frozenset())
    monkeypatch.setattr(cache, "get_negatives", lambda *args: frozenset())
    monkeypatch.setattr(repo, "product_groups", lambda ids: {})
    monkeypatch.setattr(live_remap, "remap_recs_to_live", lambda recs: recs)
    histories = {A: {101: 4}, B: {202: 9}, None: {101: 4, 202: 9}}

    def frequency(cid, as_of, client_agreement_net_id=None):
        return histories[client_agreement_net_id]

    monkeypatch.setattr(repo, "product_frequency", frequency)
    monkeypatch.setattr(copurchase, "_client_products_with_freq", frequency)
    monkeypatch.setattr(
        repo,
        "product_last_purchase",
        lambda cid, as_of, **kw: {pid: datetime(2026, 9, 1) for pid in frequency(cid, as_of, **kw)},
    )
    monkeypatch.setattr(
        repo, "owned_live_product_ids", lambda cid, as_of, **kw: set(frequency(cid, as_of, **kw))
    )
    stock_calls = []

    def stock(ids, client_agreement_net_id=None):
        stock_calls.append(client_agreement_net_id)
        return set(ids)

    monkeypatch.setattr(repo, "in_stock_product_ids", stock)
    monkeypatch.setattr(cache, "health", lambda: True)
    entries = {}
    monkeypatch.setattr(cache, "get", lambda key: entries.get(key))
    monkeypatch.setattr(cache, "set", lambda key, value, ttl=None: entries.__setitem__(key, value) or True)
    return entries, stock_calls


def test_a_b_a_uses_separate_history_and_cache(scoped_data):
    entries, stock_calls = scoped_data
    results = [
        service.get_recommendations(
            7,
            AS_OF,
            top_n=1,
            include_discovery=False,
            client_agreement_net_id=uid,
        )
        for uid in (A.upper(), B, A)
    ]
    assert [[r.product_id for r in result.recommendations] for result in results] == [[101], [202], [101]]
    assert [result.cached for result in results] == [False, False, True]
    assert [result.client_agreement_net_id for result in results] == [A, B, A]
    assert len(entries) == 2
    assert set(stock_calls) == {A, B}


def test_wrong_agreement_cached_under_right_key_is_rejected(scoped_data):
    entries, _ = scoped_data
    result = service.get_recommendations(
        7, AS_OF, top_n=1, include_discovery=False, client_agreement_net_id=B
    )
    entries[cache.make_key(7, AS_OF, 1, False, client_agreement_net_id=A)] = result.model_dump(mode="json")
    with pytest.raises(ValueError, match="identity mismatch"):
        service.get_recommendations(7, AS_OF, top_n=1, include_discovery=False, client_agreement_net_id=A)


@pytest.mark.parametrize("endpoint", ["/recommend", "/recommend/copurchase"])
def test_foreign_or_inactive_agreement_fails_even_if_customer_exists(scoped_data, endpoint):
    response = TestClient(app).post(endpoint, json={"customer_id": 8, "client_agreement_net_id": A})
    assert response.status_code == 404


def test_copurchase_uses_selected_agreement_history_and_ownership(scoped_data, monkeypatch):
    monkeypatch.setattr(
        copurchase, "_cooccurring_products", lambda *args, **kw: {101: 0.8, 202: 0.7, 303: 0.6}
    )
    results = [
        copurchase.recommend(7, AS_OF, include_owned=False, client_agreement_net_id=uid) for uid in (A, B)
    ]
    assert 101 not in {r.product_id for r in results[0].recommendations}
    assert 202 in {r.product_id for r in results[0].recommendations}
    assert 202 not in {r.product_id for r in results[1].recommendations}
    assert 101 in {r.product_id for r in results[1].recommendations}
    assert [r.client_agreement_net_id for r in results] == [A, B]


def test_worker_warms_each_agreement_and_legacy_scope(scoped_data, monkeypatch):
    monkeypatch.setattr(worker, "active_clients", lambda *args: [7])
    monkeypatch.setattr(worker, "_check_identity_churn", lambda *args: None)
    monkeypatch.setattr(copurchase, "_cooccurring_products", lambda *args, **kw: {})
    result = worker.run(AS_OF, top_n=1, include_discovery=False)
    assert result["agreements"] == 2
    assert result["contexts"] == result["ok"] == result["copurchase_ok"] == 3
    assert result["failed"] == result["copurchase_failed"] == 0
    entries, _ = scoped_data
    for uid in (None, A, B):
        assert cache.make_key(7, AS_OF, 1, False, client_agreement_net_id=uid) in entries
        assert cache.make_copurchase_key(7, AS_OF, 1, client_agreement_net_id=uid) in entries


@pytest.mark.parametrize("failed,cop_failed", [(1, 0), (0, 1)])
def test_partial_warm_failure_schedules_retry(monkeypatch, failed, cop_failed):
    from zoneinfo import ZoneInfo

    jobs = []
    fake = SimpleNamespace(timezone=ZoneInfo("Europe/Kyiv"), add_job=lambda *args, **kw: jobs.append(kw))
    monkeypatch.setattr(
        worker, "run", lambda: {"clients": 3, "ok": 2, "failed": failed, "copurchase_failed": cop_failed}
    )
    scheduler._job(fake)
    assert len(jobs) == 1
    assert jobs[0]["id"] == "daily_warm_retry"


def test_contract_key_isolation_and_case_normalization():
    keys = {
        cache.make_key(7, AS_OF, 25, True, region, uid) for uid in (None, A, B) for region in (True, False)
    }
    assert len(keys) == 6
    assert cache.make_copurchase_key(7, AS_OF, 25, A.upper()) == cache.make_copurchase_key(7, AS_OF, 25, A)


def test_worker_does_not_count_failed_cache_writes_as_updates(scoped_data, monkeypatch):
    monkeypatch.setattr(worker, "active_clients", lambda *args: [7])
    monkeypatch.setattr(worker, "_check_identity_churn", lambda *args: None)
    monkeypatch.setattr(copurchase, "_cooccurring_products", lambda *args, **kw: {})
    monkeypatch.setattr(cache, "set", lambda *args, **kw: False)
    result = worker.run(AS_OF, top_n=1, include_discovery=False)
    assert result["ok"] == result["copurchase_ok"] == 0
    assert result["failed"] == result["copurchase_failed"] == 3


def test_worker_requires_working_cache_before_computing(monkeypatch):
    monkeypatch.setattr(cache, "health", lambda: False)
    with pytest.raises(RuntimeError, match="cannot persist"):
        worker.run(AS_OF)
