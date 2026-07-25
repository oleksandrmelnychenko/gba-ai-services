"""Read-only DB access: pooled SQLAlchemy engine + parameterized query helper.

Hardened (mirrors gba-solvency): no hardcoded creds, parameterized queries (no f-string SQL),
pool with pre-ping + recycle. Transient connection/login blips (MSSQL 18456 login failure,
DB-Lib 20002 connect failure, resets) are retried with a short backoff so a blip never 500s a
console price hint; statement errors and timeouts are NOT retried.
"""
from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError

from app.core.config import get_settings
from app.core.logging import get_logger

log = get_logger("db")

_engine: Engine | None = None

_RETRY_ATTEMPTS = 3
_RETRY_BACKOFF_S = 0.3
_TRANSIENT_MARKERS = (
    "18456",
    "login failed",
    "20002",
    "adaptive server connection failed",
    "connection reset",
    "read from the server failed",
    "write to the server failed",
    "dbproc is dead",
)


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


def _is_transient(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(marker in msg for marker in _TRANSIENT_MARKERS)


def _fetch(sql: str, params: Mapping[str, Any] | None, scalars: bool) -> list[Any]:
    for attempt in range(1, _RETRY_ATTEMPTS + 1):
        try:
            with get_engine().connect() as conn:
                result = conn.execute(text(sql), params or {})
                if scalars:
                    return [row[0] for row in result.fetchall()]
                cols = list(result.keys())
                return [dict(zip(cols, row, strict=True)) for row in result.fetchall()]
        except DBAPIError as exc:
            if attempt == _RETRY_ATTEMPTS or not _is_transient(exc):
                raise
            log.warning("transient_db_error_retry", attempt=attempt, error=str(exc))
            time.sleep(_RETRY_BACKOFF_S * attempt)
    raise RuntimeError("unreachable")


def query(sql: str, params: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """Run a parameterized read query. Use :name placeholders, pass params dict."""
    return _fetch(sql, params, scalars=False)


def query_scalars(sql: str, params: Mapping[str, Any] | None = None) -> list[Any]:
    return _fetch(sql, params, scalars=True)


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
