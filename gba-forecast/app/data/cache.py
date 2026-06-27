"""Redis cache — one key scheme, graceful degradation. Forecast namespace.

Key scheme: forecast:{ver}:{kind}:{id}:{months}  (kind = client|product|client_product)
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

_VER = "v1"
_client: redis.Redis | None = None
_unavailable = False
_last_failure_at = 0.0


def _get_client() -> redis.Redis | None:
    global _client, _unavailable, _last_failure_at
    s = get_settings()
    if _unavailable:
        retry_after = max(1, s.redis_retry_interval_seconds)
        if time.monotonic() - _last_failure_at < retry_after:
            return None
        _unavailable = False
    if _client is None:
        try:
            _client = redis.Redis(
                host=s.redis_host,
                port=s.redis_port,
                db=s.redis_db,
                decode_responses=True,
                socket_connect_timeout=2,
                socket_timeout=2,
            )
            _client.ping()
            log.info("redis_connected", host=s.redis_host, port=s.redis_port, db=s.redis_db)
        except Exception as exc:  # noqa: BLE001
            log.warning("redis_unavailable", error=str(exc))
            _client = None
            _unavailable = True
            _last_failure_at = time.monotonic()
    return _client


def make_key(kind: str, entity_id: int | str, months: int, *parts: object) -> str:
    suffix = ":".join(_key_part(part) for part in parts if part is not None)
    base = f"forecast:{_VER}:{kind}:{_key_part(entity_id)}:{months}"
    return f"{base}:{suffix}" if suffix else base


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


def _key_part(value: object) -> str:
    return str(value).replace(":", "_")
