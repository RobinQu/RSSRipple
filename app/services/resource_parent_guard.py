"""Turso parent-row write barriers for resource-retaining child records.

Turso CONCURRENT transactions do not give child FK inserts PostgreSQL's
parent KEY SHARE lock. A cleanup using an older write snapshot can otherwise
commit a parent deletion without seeing a newly committed child. Touching the
parent's row version makes that schedule a retryable write-write conflict.
"""

from sqlalchemy import inspect

_CHILDREN = {
    "resource_work_links": "resource_id",
    "resource_file_assignments": "resource_id",
    "download_tasks": "file_resource_id",
}


def ensure_resource_parent_guards(connection) -> None:
    """Install idempotent guards on fresh and upgraded embedded databases."""
    if connection.dialect.driver != "aioturso":
        return
    tables = set(inspect(connection).get_table_names())
    if "file_resources" not in tables:
        return
    for table, foreign_key in _CHILDREN.items():
        if table not in tables:
            continue
        for operation in ("INSERT", "UPDATE"):
            # Identifiers are fixed internal names. The equal-value UPDATE is
            # intentional: advance Turso's MVCC row version without changing
            # a resource's identity, timestamp, or user-visible metadata.
            connection.exec_driver_sql(
                f"CREATE TRIGGER IF NOT EXISTS trg_resource_parent_{table}_{operation.lower()} "
                f"BEFORE {operation} ON {table} BEGIN "
                f"UPDATE file_resources SET id = id WHERE id = NEW.{foreign_key}; END"
            )


def on_metadata_created(_metadata, connection, **_kwargs) -> None:
    ensure_resource_parent_guards(connection)


def drop_resource_parent_guards(connection) -> None:
    """Temporarily remove our guards inside an atomic parent-table rebuild.

    Turso validates triggers on other tables during RENAME; they cannot refer
    to the temporarily absent parent. The caller reinstalls before commit,
    and rolling back that DDL transaction restores the original guards.
    """
    if connection.dialect.driver != "aioturso":
        return
    for table in _CHILDREN:
        for operation in ("insert", "update"):
            connection.exec_driver_sql(f"DROP TRIGGER IF EXISTS trg_resource_parent_{table}_{operation}")
