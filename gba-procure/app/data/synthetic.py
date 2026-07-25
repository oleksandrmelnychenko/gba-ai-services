"""Synthetic debt product («Ввід боргів») — resolved dynamically so DB re-mints survive.

The row is periodically re-minted under a new ID (the old hardcoded 25422404 is dead),
so the exclusion ID is resolved from dbo.Product at first use, cached, and refreshed
hourly. The SYNTHETIC_PRODUCT_ID env var, if set, overrides resolution entirely.
"""
from __future__ import annotations

import threading
import time

from app.core.config import get_settings
from app.core.logging import get_logger

log = get_logger("synthetic")

FALLBACK_SYNTHETIC_PRODUCT_ID = 29555414
_REFRESH_S = 3600.0

_lock = threading.Lock()
_cached: int | None = None
_resolved_at = 0.0


def synthetic_product_id() -> int:
    global _cached, _resolved_at
    override = get_settings().synthetic_product_id
    if override:
        return int(override)
    if _cached is not None and time.monotonic() - _resolved_at < _REFRESH_S:
        return _cached
    with _lock:
        if _cached is not None and time.monotonic() - _resolved_at < _REFRESH_S:
            return _cached
        from app.data.db import query

        try:
            rows = query(
                "SELECT TOP 1 ID FROM dbo.Product "
                "WHERE Name = N'Ввід боргів' AND Deleted = 0 ORDER BY ID DESC"
            )
            if rows:
                resolved = int(rows[0]["ID"])
                if resolved != _cached:
                    log.info("synthetic_product_resolved", product_id=resolved)
                _cached = resolved
            elif _cached is None:
                log.warning(
                    "synthetic_product_not_found", fallback=FALLBACK_SYNTHETIC_PRODUCT_ID
                )
                _cached = FALLBACK_SYNTHETIC_PRODUCT_ID
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "synthetic_product_resolve_failed",
                error=str(exc),
                using=_cached or FALLBACK_SYNTHETIC_PRODUCT_ID,
            )
            if _cached is None:
                _cached = FALLBACK_SYNTHETIC_PRODUCT_ID
        _resolved_at = time.monotonic()
        return _cached
