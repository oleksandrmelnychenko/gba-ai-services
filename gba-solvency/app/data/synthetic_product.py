"""Dynamic resolution of the synthetic 1С debt-entry ProductID(s) («Ввід боргів»).

The synthetic product row gets RE-MINTED by catalog re-syncs (old hardcoded 25422404 died and
was replaced by 29555414), so a static env ID silently stops matching and re-inflates turnover.
Canonical rule: resolve the live ID from the DB at startup (cached, refreshed hourly or on a
resolution miss), unioned with any env-configured IDs (SYNTHETIC_LINE_PRODUCT_ID acts as an
override/safety net for historical rows). This survives future re-mints.
"""
from __future__ import annotations

import threading
import time

from app.core.config import get_settings
from app.core.logging import get_logger
from app.data.db import query

log = get_logger("synthetic_product")

_REFRESH_S = 3600.0
_lock = threading.Lock()
_state: dict = {"at": 0.0, "resolved": None}


def _resolve_live_id() -> int | None:
    rows = query(
        """
        SELECT TOP 1 ID FROM dbo.Product
        WHERE Name = :nm AND Deleted = 0
        ORDER BY ID DESC
        """,
        {"nm": get_settings().synthetic_line_product_name},
    )
    return int(rows[0]["ID"]) if rows else None


def synthetic_product_ids() -> set[int]:
    """Effective synthetic-ID set: env-configured IDs ∪ the live DB-resolved ID.

    Cached for an hour; a resolution miss (DB down / no row) keeps the last known value and
    retries on the next call. Never raises — worst case it degrades to the env-configured set.
    """
    now = time.monotonic()
    with _lock:
        resolved = _state["resolved"]
        fresh = resolved is not None and (now - _state["at"]) < _REFRESH_S
    if not fresh:
        try:
            live = _resolve_live_id()
        except Exception as exc:  # noqa: BLE001
            log.warning("synthetic_id_resolve_failed", error=str(exc))
            live = None
        if live is not None:
            with _lock:
                if _state["resolved"] != live:
                    log.info("synthetic_id_resolved", product_id=live)
                _state["resolved"] = live
                _state["at"] = now
            resolved = live
    ids = set(get_settings().synthetic_line_product_ids)
    if resolved is not None:
        ids.add(resolved)
    return ids
