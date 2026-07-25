"""Read-only DB access: pooled SQLAlchemy engine + parameterized query helper.

Hardened (mirrors gba-reco): no hardcoded creds, parameterized queries (no f-string SQL),
pool with pre-ping + recycle. Connection acquisition retries transient MSSQL login
failures (error 18456 blips during the nightly 1C sync) with a short backoff.
"""
from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import DBAPIError

from app.core.config import get_settings

_CONNECT_ATTEMPTS = 3
_CONNECT_BACKOFF_S = 0.3

_engine: Engine | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        s = get_settings()
        _engine = create_engine(
            s.sqlalchemy_url,
            pool_size=s.db_pool_size,
            max_overflow=s.db_max_overflow,
            pool_pre_ping=True,
            pool_recycle=3600,
            pool_timeout=s.query_timeout,
            connect_args={"timeout": s.query_timeout, "login_timeout": 10},
            echo=False,
        )
    return _engine


def connect() -> Connection:
    """Acquire a pooled connection, retrying transient login failures (MSSQL 18456 blips
    during the nightly 1C sync) with a short exponential backoff. Read-only service: a
    retried connect never re-runs a query."""
    last: Exception | None = None
    for attempt in range(_CONNECT_ATTEMPTS):
        try:
            return get_engine().connect()
        except DBAPIError as exc:
            last = exc
            if attempt < _CONNECT_ATTEMPTS - 1:
                time.sleep(_CONNECT_BACKOFF_S * (2**attempt))
    raise last  # type: ignore[misc]


def query(sql: str, params: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """Run a parameterized read query. Use :name placeholders, pass params dict."""
    with connect() as conn:
        result = conn.execute(text(sql), params or {})
        cols = list(result.keys())
        return [dict(zip(cols, row, strict=True)) for row in result.fetchall()]


def query_scalars(sql: str, params: Mapping[str, Any] | None = None) -> list[Any]:
    with connect() as conn:
        return [row[0] for row in conn.execute(text(sql), params or {}).fetchall()]


def in_clause(name: str, values: Sequence[Any]) -> tuple[str, dict[str, Any]]:
    """Build a safe, parameterized IN clause: returns ("(:p0,:p1,...)", {p0:..,})."""
    keys = [f"{name}{i}" for i in range(len(values))]
    placeholder = "(" + ",".join(f":{k}" for k in keys) + ")" if keys else "(NULL)"
    return placeholder, dict(zip(keys, values, strict=True))


def dispose() -> None:
    global _engine
    if _engine is not None:
        _engine.dispose()
        _engine = None
