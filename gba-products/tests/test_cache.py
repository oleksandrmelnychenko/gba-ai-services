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
    assert key.endswith(":assortment:portfolio:2025-12-01")


def test_cache_key_changes_when_source_history_floor_changes(monkeypatch):
    settings = get_settings()
    first = cache.make_key("assortment", "portfolio", "2026-07-25")

    monkeypatch.setattr(settings, "source_history_start_date", date(2025, 2, 1))
    second = cache.make_key("assortment", "portfolio", "2026-07-25")

    assert first != second
