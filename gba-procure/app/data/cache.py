"""Redis cache — one key scheme, graceful degradation. Procurement namespace.

Key scheme: procure:{ver}:{kind}:{id}:{as_of}  (kind = producer|product)
If Redis is down, every call is a no-op miss — service keeps working.
"""
from __future__ import annotations

import json
import time
from typing import Any

import redis

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.metrics import METRICS

log = get_logger("cache")

# v3: /plan/cart keys became only_needed-aware (a shared v2 key could hold either
# variant's plan for 8 days) — the bump kills all stale entries; scheduler re-warms.
_VER = "v3"
_RETRY_COOLDOWN_S = 30.0
_client: redis.Redis | None = None
_unavailable_until = 0.0


def _get_client() -> redis.Redis | None:
    global _client, _unavailable_until
    if _unavailable_until and time.monotonic() < _unavailable_until:
        return None
    if _client is None:
        s = get_settings()
        try:
            _client = redis.Redis(
                host=s.redis_host, port=s.redis_port, db=s.redis_db,
                decode_responses=True, socket_connect_timeout=2, socket_timeout=2,
            )
            _client.ping()
            _unavailable_until = 0.0
            log.info("redis_connected", host=s.redis_host, port=s.redis_port, db=s.redis_db)
        except Exception as exc:  # noqa: BLE001
            # Cool-down, not a permanent flag: a Redis blip at boot must not disable
            # caching (and silently void the scheduler's warm passes) until restart.
            log.warning("redis_unavailable", error=str(exc), retry_in_s=_RETRY_COOLDOWN_S)
            _client = None
            _unavailable_until = time.monotonic() + _RETRY_COOLDOWN_S
    return _client


def make_key(kind: str, entity_id: int | str, as_of: str) -> str:
    return f"procure:{_VER}:{kind}:{entity_id}:{as_of}"


def get(key: str) -> dict[str, Any] | None:
    client = _get_client()
    if client is None:
        return None
    try:
        raw = client.get(key)
    except Exception as exc:  # noqa: BLE001
        log.warning("cache_get_failed", error=str(exc))
        return None
    if raw is None:
        METRICS.record_cache(hit=False)
        return None
    METRICS.record_cache(hit=True)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        log.warning("cache_decode_failed", key=key, error=str(exc))
        return None


def set(key: str, value: dict[str, Any], ttl: int | None = None) -> None:
    client = _get_client()
    if client is None:
        return
    ttl = ttl or get_settings().cache_ttl
    try:
        client.setex(key, ttl, json.dumps(value, default=str))
    except Exception as exc:  # noqa: BLE001
        log.warning("cache_set_failed", error=str(exc))


def invalidate_plans(producer_id: int | None = None) -> int:
    """Drop cached plans so masters/feedback edits reach the very next request.

    Cart/charts plans aggregate every producer, so they are always dropped; the
    producer-scoped plan keys are dropped for the given producer (or all when None).
    """
    client = _get_client()
    if client is None:
        return 0
    producer_part = producer_id if producer_id is not None else "*"
    patterns = (
        f"procure:{_VER}:producer:{producer_part}:*",
        f"procure:{_VER}:cart:*",
        f"procure:{_VER}:cartbudget:*",
        f"procure:{_VER}:charts:*",
    )
    try:
        keys: list[str] = []
        for pattern in patterns:
            keys.extend(client.scan_iter(match=pattern, count=200))
        deleted = client.delete(*keys) if keys else 0
        log.info("cache_plans_invalidated", producer_id=producer_id, deleted=deleted)
        return deleted
    except Exception as exc:  # noqa: BLE001
        log.warning("cache_invalidate_failed", producer_id=producer_id, error=str(exc))
        return 0


def health() -> bool:
    client = _get_client()
    if client is None:
        return False
    try:
        return bool(client.ping())
    except Exception:  # noqa: BLE001
        return False
