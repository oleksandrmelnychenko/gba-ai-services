"""Pure cache-key tests."""
from __future__ import annotations

from datetime import date

from app.core.config import get_settings
from app.core.history import history_contract_fingerprint
from app.data import cache


def test_cache_key_includes_model_version():
    key = cache.make_key("assortment", "portfolio", "2025-12-01")

    assert key.startswith(f"products:{get_settings().model_version}:")
    assert history_contract_fingerprint(get_settings().source_history_start_date) in key
    assert key.endswith(f":assortment:portfolio:2025-12-01:{cache.refresh_generation()}")


def test_cache_key_changes_when_source_history_floor_changes(monkeypatch):
    settings = get_settings()
    first = cache.make_key("assortment", "portfolio", "2026-07-25")

    monkeypatch.setattr(settings, "source_history_start_date", date(2025, 2, 1))
    second = cache.make_key("assortment", "portfolio", "2026-07-25")

    assert first != second


def test_cache_recovers_after_startup_redis_failure(monkeypatch):
    from unittest.mock import Mock
    monkeypatch.setattr(cache, "_client", None)
    monkeypatch.setattr(cache, "_unavailable_until", 0.0)
    monkeypatch.setattr(cache.time, "monotonic", lambda: 100.0)
    client = Mock()
    client.ping.return_value = True
    factory = Mock(side_effect=[ConnectionError("unavailable"), client])
    monkeypatch.setattr(cache.redis, "Redis", factory)
    assert cache._get_client() is None
    assert cache._get_client() is None
    assert factory.call_count == 1
    monkeypatch.setattr(cache.time, "monotonic", lambda: 131.0)
    assert cache._get_client() is client
