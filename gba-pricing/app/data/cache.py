"""Redis cache — ONE documented key scheme.

Key scheme (single source of truth):
    price:{model_version}:v2:{product}:{agreement}:{asof}:culture={culture}:vat={vat}:margin={margin}:window={window}:fx={fx_date}:elas={elasticity}
where {product} is the product id, {agreement} is the client-agreement NetUID. The model
version is embedded so a model bump auto-invalidates old entries. The serving options are
embedded because the same product/agreement/date can legitimately produce a different result
when VAT, culture, margin, window, FX pinning, or the secondary elasticity signal changes.

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

_client: redis.Redis | None = None
_unavailable_until = 0.0


def _mark_unavailable(event: str, exc: Exception) -> None:
    global _client, _unavailable_until
    s = get_settings()
    _client = None
    _unavailable_until = time.monotonic() + s.redis_retry_cooldown_seconds
    log.warning(event, error=str(exc), retry_after_seconds=s.redis_retry_cooldown_seconds)


def _model_version() -> str:
    return get_settings().model_version


def _get_client() -> redis.Redis | None:
    global _client
    if _client is None:
        if time.monotonic() < _unavailable_until:
            return None
        s = get_settings()
        try:
            _client = redis.Redis(
                host=s.redis_host, port=s.redis_port, db=s.redis_db,
                decode_responses=True, socket_connect_timeout=2, socket_timeout=2,
            )
            _client.ping()
            log.info("redis_connected", host=s.redis_host, port=s.redis_port, db=s.redis_db)
        except Exception as exc:  # noqa: BLE001
            _mark_unavailable("redis_unavailable", exc)
    return _client


def _key_part(value: object) -> str:
    return str(value).strip().replace(":", "_") or "default"


def make_key(
    product: int | str,
    agreement: str,
    as_of: str,
    *,
    culture: str,
    with_vat: bool,
    target_margin_pct: float,
    window_months: int,
    fx_date: str,
    elasticity_enabled: bool,
) -> str:
    parts = [
        "price",
        _model_version(),
        "v2",
        _key_part(product),
        _key_part(agreement),
        _key_part(as_of),
        f"culture={_key_part(culture.lower())}",
        f"vat={1 if with_vat else 0}",
        f"margin={_key_part(round(float(target_margin_pct), 6))}",
        f"window={int(window_months)}",
        f"fx={_key_part(fx_date)}",
        f"elas={1 if elasticity_enabled else 0}",
    ]
    return ":".join(parts)


def get(key: str) -> dict[str, Any] | None:
    client = _get_client()
    if client is None:
        return None
    try:
        raw = client.get(key)
    except Exception as exc:  # noqa: BLE001
        _mark_unavailable("cache_get_failed", exc)
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
        _mark_unavailable("cache_set_failed", exc)


def invalidate(product: int | str, agreement: str) -> int:
    client = _get_client()
    if client is None:
        return 0
    try:
        patterns = [
            f"price:{_model_version()}:v2:{product}:{agreement}:*",
            f"price:{_model_version()}:{product}:{agreement}:*",
        ]
        keys = []
        for pattern in patterns:
            keys.extend(client.scan_iter(match=pattern, count=200))
        return client.delete(*keys) if keys else 0
    except Exception as exc:  # noqa: BLE001
        _mark_unavailable("cache_invalidate_failed", exc)
        return 0


def health() -> bool:
    client = _get_client()
    if client is None:
        return False
    try:
        return bool(client.ping())
    except Exception as exc:  # noqa: BLE001
        _mark_unavailable("redis_health_failed", exc)
        return False
