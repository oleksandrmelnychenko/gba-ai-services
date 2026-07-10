"""Redis cache — one key scheme, graceful degradation. Products namespace.

Key scheme: products:{model_version}:{kind}:{id}:{as_of}  (kind = product|assortment)
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
            # Cool-down, not a permanent flag: a Redis blip must not force every
            # request onto the full portfolio build for the process lifetime.
            log.warning("redis_unavailable", error=str(exc), retry_in_s=_RETRY_COOLDOWN_S)
            _client = None
            _unavailable_until = time.monotonic() + _RETRY_COOLDOWN_S
    return _client


def make_key(kind: str, entity_id: int | str, as_of: str) -> str:
    return f"products:{get_settings().model_version}:{kind}:{entity_id}:{as_of}"


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
        # Treat a corrupt value as a miss — otherwise one bad key 500s every
        # request that touches it until TTL expiry (get raises before rebuild).
        log.warning("cache_decode_failed", key=key, error=str(exc))
        return None


def set(key: str, value: dict[str, Any], ttl: int | None = None) -> None:
    client = _get_client()
    if client is None:
        return
    ttl = ttl or get_settings().cache_ttl
    try:
        client.set(key, json.dumps(value, default=str), ex=ttl)
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
