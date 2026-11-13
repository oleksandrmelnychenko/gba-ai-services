"""The ABC map must follow the source state the plan is built from."""

from __future__ import annotations

import pytest

from app.data import cache
from app.data import supply_repository as repo
from app.services.classify import service as classify

AS_OF = "2026-06-15"


@pytest.fixture
def store(monkeypatch):
    data: dict[str, object] = {}
    reads = {"revenue": 0}
    revenue = {1: 900.0, 2: 90.0, 3: 10.0}

    def read_revenue(as_of, history_days):
        reads["revenue"] += 1
        return dict(revenue)

    monkeypatch.setattr(cache, "get", lambda key: data.get(key))
    monkeypatch.setattr(cache, "set", lambda key, value, ttl=None: data.__setitem__(key, value) or True)
    monkeypatch.setattr(repo, "all_products_revenue_eur", read_revenue)
    return data, reads, revenue


def _publish(data, fingerprint):
    data[cache.make_key("readiness", "source", AS_OF)] = {
        "as_of": AS_OF,
        "ready": True,
        "source_fingerprint": fingerprint,
    }


def test_abc_is_reused_while_the_source_fingerprint_is_unchanged(store):
    data, reads, _ = store
    _publish(data, "epoch-1")
    first = classify.get_abc_map(AS_OF, 120)
    assert classify.get_abc_map(AS_OF, 120) == first == {1: "A", 2: "B", 3: "C"}
    assert reads["revenue"] == 1


def test_abc_is_reclassified_when_the_source_fingerprint_changes(store):
    data, reads, revenue = store
    _publish(data, "epoch-1")
    assert classify.get_abc_map(AS_OF, 120)[3] == "C"
    revenue.update({1: 10.0, 3: 900.0})
    assert classify.get_abc_map(AS_OF, 120)[3] == "C"
    _publish(data, "epoch-2")
    assert classify.get_abc_map(AS_OF, 120) == {3: "A", 2: "B", 1: "C"}
    assert reads["revenue"] == 2


def test_abc_without_a_readiness_snapshot_keeps_the_daily_cache(store):
    _, reads, _ = store
    classify.get_abc_map(AS_OF, 120)
    classify.get_abc_map(AS_OF, 120)
    assert reads["revenue"] == 1


def test_abc_ignores_a_cached_map_without_a_source_epoch_record(store):
    data, reads, _ = store
    key = cache.make_key("abc", "120:effective:120", AS_OF)
    data[key] = {"1": "C"}
    assert classify.get_abc_map(AS_OF, 120)[1] == "A"
    assert reads["revenue"] == 1

