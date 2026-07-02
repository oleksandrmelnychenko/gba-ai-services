"""Read-only DB access: pooled SQLAlchemy engine + parameterized query helper.

Hardened vs the prototype: no hardcoded creds, parameterized queries (no f-string SQL),
pool with pre-ping + recycle.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from app.core.config import get_settings

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
            echo=False,
        )
    return _engine


def query(sql: str, params: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """Run a parameterized read query. Use :name placeholders, pass params dict."""
    with get_engine().connect() as conn:
        result = conn.execute(text(sql), params or {})
        cols = list(result.keys())
        return [dict(zip(cols, row, strict=True)) for row in result.fetchall()]


def query_scalars(sql: str, params: Mapping[str, Any] | None = None) -> list[Any]:
    with get_engine().connect() as conn:
        return [row[0] for row in conn.execute(text(sql), params or {}).fetchall()]


def in_clause(name: str, values: Sequence[Any]) -> tuple[str, dict[str, Any]]:
    """Build a safe, parameterized IN clause: returns ("(:p0,:p1,...)", {p0:..,}).
    Avoids the prototype's string-concatenated IN lists."""
    keys = [f"{name}{i}" for i in range(len(values))]
    placeholder = "(" + ",".join(f":{k}" for k in keys) + ")" if keys else "(NULL)"
    return placeholder, dict(zip(keys, values, strict=True))


def dispose() -> None:
    global _engine
    if _engine is not None:
        _engine.dispose()
        _engine = None
