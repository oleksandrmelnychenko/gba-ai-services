"""Redis cache — one key scheme, graceful degradation. Procurement namespace.

Key scheme: procure:{ver}:{kind}:{id}:{as_of}  (kind = producer|product)
If Redis is down, every call is a no-op miss — service keeps working.
"""
from __future__ import annotations

import json
from typing import Any

import redis

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.metrics import METRICS

log = get_logger("cache")

_VER = "v2"
_client: redis.Redis | None = None
_unavailable = False


def _get_client() -> redis.Redis | None:
    global _client, _unavailable
    if _unavailable:
        return None
    if _client is None:
        s = get_settings()
        try:
            _client = redis.Redis(
                host=s.redis_host, port=s.redis_port, db=s.redis_db,
                decode_responses=True, socket_connect_timeout=2, socket_timeout=2,
            )
            _client.ping()
            log.info("redis_connected", host=s.redis_host, port=s.redis_port, db=s.redis_db)
        except Exception as exc:  # noqa: BLE001
            log.warning("redis_unavailable", error=str(exc))
            _client = None
            _unavailable = True
    return _client


def make_key(kind: str, entity_id: int, as_of: str) -> str:
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
    return json.loads(raw)


def set(key: str, value: dict[str, Any], ttl: int | None = None) -> None:
    client = _get_client()
    if client is None:
        return
    ttl = ttl or get_settings().cache_ttl
    try:
        client.setex(key, ttl, json.dumps(value, default=str))
    except Exception as exc:  # noqa: BLE001
        log.warning("cache_set_failed", error=str(exc))


def health() -> bool:
    client = _get_client()
    if client is None:
        return False
    try:
        return bool(client.ping())
    except Exception:  # noqa: BLE001
        return False
