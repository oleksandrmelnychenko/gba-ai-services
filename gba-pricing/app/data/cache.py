"""Redis cache — ONE documented key scheme.

Key scheme (single source of truth):
    price:{model_version}:{product}:{agreement}:{asof}:{margin}:{vat}:{culture}
where {product} is the product id, {agreement} is the client-agreement NetUID, and the pricing
params (target margin, VAT flag, culture) are folded in so a cached result is only reused for the
exact params it was computed with. The model version is embedded so a model bump auto-invalidates
old entries.

Graceful degradation: if Redis is down, every call is a no-op miss — the service still works
(just uncached). Never let cache failure break a recommendation.
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


def _model_version() -> str:
    return get_settings().model_version


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
            # Cool-down, not a permanent flag: a Redis blip at boot must not
            # disable price caching for the process lifetime.
            log.warning("redis_unavailable", error=str(exc), retry_in_s=_RETRY_COOLDOWN_S)
            _client = None
            _unavailable_until = time.monotonic() + _RETRY_COOLDOWN_S
    return _client


def make_key(product: int | str, agreement: str, as_of: str,
             target_margin_pct: float, with_vat: bool, culture: str) -> str:
    return (
        f"price:{_model_version()}:{product}:{agreement.lower()}:{as_of}"
        f":{target_margin_pct}:{int(with_vat)}:{culture}"
    )


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


def invalidate(product: int | str, agreement: str) -> int:
    client = _get_client()
    if client is None:
        return 0
    pattern = f"price:{_model_version()}:{product}:{str(agreement).lower()}:*"
    try:
        keys = list(client.scan_iter(match=pattern, count=200))
        return client.delete(*keys) if keys else 0
    except Exception as exc:  # noqa: BLE001
        log.warning("cache_invalidate_failed", product=product, error=str(exc))
        return 0


def health() -> bool:
    client = _get_client()
    if client is None:
        return False
    try:
        return bool(client.ping())
    except Exception:  # noqa: BLE001
        return False
