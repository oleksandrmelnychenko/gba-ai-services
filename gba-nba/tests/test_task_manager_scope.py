"""Task assignment population is distinct from current-book financial attribution."""
import os
from uuid import uuid4

import mongomock
import pytest

from app.data import mongo, signals_repository
from app.services import task_delegation


def test_only_active_assignees_extend_task_scope_without_changing_financial_scope(monkeypatch):
    db = mongomock.MongoClient()["task_scope_"+uuid4().hex]
    monkeypatch.setattr(mongo, "get_db", lambda: db)
    db.tasks.insert_many([
        {"manager_id": 3, "status": "open"},
        {"manager_id": 4, "status": "snoozed"},
        {"manager_id": 5, "status": "done"},
        {"manager_id": 6, "status": "superseded"},
    ])
    observed = []
    monkeypatch.setattr(signals_repository, "task_manager_ids",
                        lambda ids: observed.append(sorted(ids)) or [1, 2, 3])
    monkeypatch.setattr(signals_repository, "all_managers", lambda: [1])
    assert task_delegation.manager_ids() == [1, 2, 3]
    assert observed == [[3, 4]]
    assert signals_repository.all_managers() == [1]


def test_task_manager_scope_sql_retains_zero_book_sellers_and_validates_assignee_users(monkeypatch):
    env_file = os.environ.get("NBA_SQL_FIXTURE_ENV_FILE")
    if not env_file:
        pytest.skip("DEV reader env required for session-local SQL fixture")
    from sqlalchemy import create_engine, text

    from app.core.config import Settings
    settings = Settings(_env_file=env_file)
    assert settings.db_name == "ConcordDb_V5"
    engine = create_engine(settings.sqlalchemy_url)
    with engine.connect() as connection:
        can_write = connection.execute(
            text("SELECT HAS_PERMS_BY_NAME(DB_NAME(),'DATABASE','INSERT')")).scalar()
        assert can_write == 0
        connection.exec_driver_sql("""
          CREATE TABLE #User(ID bigint,Deleted bit,UserRoleID bigint);
          CREATE TABLE #Role(ID bigint,UserRoleType int);
          CREATE TABLE #Client(ID bigint,MainManagerID bigint,Deleted bit);
          INSERT #Role VALUES(1,0),(2,7);
          INSERT #User VALUES(1,0,2),(2,0,1),(3,0,2),(4,1,1),(5,0,2),(6,0,2),(7,1,1),(8,0,2);
          INSERT #Client VALUES(1,1,0),(2,4,0),(3,8,1);
        """)
        def query(sql, params=None):
            sql = (sql.replace("dbo.[User]", "#User").replace("dbo.UserRole", "#Role")
                   .replace("dbo.Client", "#Client"))
            return [dict(row) for row in connection.execute(text(sql), params or {}).mappings()]
        monkeypatch.setattr(signals_repository, "query", query)
        assert signals_repository.all_managers() == [1]
        assert signals_repository.task_manager_ids([3, 4]) == [1, 2, 3]
        assert signals_repository.task_manager_ids([]) == [1, 2]
    engine.dispose()
