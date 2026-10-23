"""Environment identity checked against the actual SQL connection, without business writes."""
from app.core.config import Settings
from app.data.db import get_engine


def validate_source_database(settings: Settings) -> str:
    with get_engine().connect() as connection:
        row = connection.exec_driver_sql("""
            SELECT DB_NAME() AS database_name,
              IS_ROLEMEMBER('db_datareader') AS is_reader,
              IS_ROLEMEMBER('db_owner') AS is_owner,
              IS_SRVROLEMEMBER('sysadmin') AS is_sysadmin,
              HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'INSERT') AS can_insert,
              HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'UPDATE') AS can_update,
              HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'DELETE') AS can_delete
        """).mappings().one()
    if row["database_name"] != settings.db_name:
        raise RuntimeError("products_source_database_mismatch")
    if settings.source_environment == "qa" and (
        row["is_reader"] != 1 or any(row[key] != 0 for key in (
            "is_owner", "is_sysadmin", "can_insert", "can_update", "can_delete"
        ))
    ):
        raise RuntimeError("products_qa_database_must_be_readonly")
    return row["database_name"]
