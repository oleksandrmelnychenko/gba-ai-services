from __future__ import annotations

import pytest

from app.data import pricing_repository as repo
from app.services import competitor_search as comp


@pytest.fixture(autouse=True)
def _reset_process_caches():
    repo.reset_segment_cache()
    yield
    repo.reset_segment_cache()


@pytest.fixture(autouse=True)
def competitor_cache(monkeypatch):
    store: dict = {"data": {}, "ttl": {}}

    def fake_set(key, value, ttl):
        store["data"][key] = value
        store["ttl"][key] = ttl

    monkeypatch.setattr(comp, "_cache_get", lambda key: store["data"].get(key))
    monkeypatch.setattr(comp, "_cache_set", fake_set)
    return store
