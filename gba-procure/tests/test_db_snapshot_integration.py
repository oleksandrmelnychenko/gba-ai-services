"""Exercise pooled snapshot batches without writing application tables."""
from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import DBAPIError

from app.core.config import get_settings
from app.data import db

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    not (os.getenv("DB_HOST") and os.getenv("DB_PASSWORD")),
    reason="DB_HOST/DB_PASSWORD required for SQL snapshot integration tests",
)]


@pytest.fixture
def single_connection_pool(monkeypatch):
    engine = create_engine(get_settings().sqlalchemy_url, pool_size=1, max_overflow=0)
    monkeypatch.setattr(db, "_engine", engine)
    yield
    engine.dispose()


def session_state():
    return db.query("""
        SELECT @@SPID AS spid, transaction_isolation_level AS isolation_level,
               OBJECT_ID('tempdb..#snapshot_batch_probe') AS temp_id
        FROM sys.dm_exec_sessions WHERE session_id = @@SPID
    """)[0]


def test_snapshot_batch_can_repeat_without_leaking_tables_or_isolation(single_connection_pool):
    before = session_state()
    for value in (7, 19):
        result = db.query(
            "EXEC sys.sp_executesql :statement, N'@value int', @value=:value",
            {"statement": """
                SET NOCOUNT ON;
                SELECT @value AS value INTO #snapshot_batch_probe;
                SELECT p.value, s.transaction_isolation_level AS isolation_level
                FROM #snapshot_batch_probe p
                CROSS JOIN sys.dm_exec_sessions s WHERE s.session_id = @@SPID;
            """, "value": value},
            isolation_level="SNAPSHOT",
        )
        assert result == [{"value": value, "isolation_level": 5}]
        assert session_state() == before
    assert before["temp_id"] is None


def test_failed_snapshot_batch_returns_a_clean_connection(single_connection_pool):
    before = session_state()
    with pytest.raises(DBAPIError, match="intentional snapshot test failure"):
        db.query(
            "EXEC sys.sp_executesql :statement",
            {"statement": """
                SET NOCOUNT ON;
                SELECT 1 AS value INTO #snapshot_batch_probe;
                THROW 50000, 'intentional snapshot test failure', 1;
            """},
            isolation_level="SNAPSHOT",
        )
    assert session_state() == before
    assert db.query("SELECT 42 AS value") == [{"value": 42}]
