"""Pure cache-key tests."""
from __future__ import annotations

from app.core.config import get_settings
from app.data import cache


def test_cache_key_includes_model_version():
    key = cache.make_key("assortment", "portfolio", "2025-12-01")

    assert key.startswith(f"products:{get_settings().model_version}:")
    assert key.endswith(":assortment:portfolio:2025-12-01")
