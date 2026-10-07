"""Turso parent-row version barriers for resource and work references.

Turso CONCURRENT child inserts do not acquire PostgreSQL's parent KEY SHARE
lock. Parent touches make cleanup/merge deletion conflict with late child
writers instead of leaving a committed reference outside the deleter's snapshot.
"""

from sqlalchemy import inspect

_CHILDREN = {
    "resource_work_links": "resource_id",
    "resource_file_assignments": "resource_id",
    "download_tasks": "file_resource_id",
}
_WORK_PARENTS = {"movies", "tv_series"}


def _guard_specs():
    """Use declared FKs so a new work-reference model cannot omit its barrier."""
    from app.database import Base

    for table, column in _CHILDREN.items():
        yield f"trg_resource_parent_{table}", table, column, "file_resources"
    for table in Base.metadata.sorted_tables:
        for column in table.columns:
            for foreign_key in column.foreign_keys:
                parent = foreign_key.column.table.name
                if parent in _WORK_PARENTS and foreign_key.column.name == "id":
                    yield f"trg_work_parent_{table.name}_{column.name}", table.name, column.name, parent


def ensure_resource_parent_guards(connection) -> None:
    """Install resource/work barriers on fresh and upgraded embedded databases.

    The historical entry point remains shared by metadata creation and schema
    repair; legacy tables without a new FK column are handled on the next pass.
    """
    if connection.dialect.driver != "aioturso":
        return
    inspector = inspect(connection)
    tables = set(inspector.get_table_names())
    columns = {}
    quote = connection.dialect.identifier_preparer.quote
    for name, table, column, parent in _guard_specs():
        if table not in tables or parent not in tables:
            continue
        if table not in columns:
            columns[table] = {item["name"] for item in inspector.get_columns(table)}
        if column not in columns[table]:
            continue
        for operation in ("INSERT", "UPDATE"):
            # Equal-value DML advances MVCC versions without changing user
            # metadata or timestamps. Quoted names come from internal models.
            connection.exec_driver_sql(
                f"CREATE TRIGGER IF NOT EXISTS {quote(name + '_' + operation.lower())} "
                f"BEFORE {operation} ON {quote(table)} BEGIN "
                f"UPDATE {quote(parent)} SET id = id WHERE id = NEW.{quote(column)}; END"
            )


def on_metadata_created(_metadata, connection, **_kwargs) -> None:
    ensure_resource_parent_guards(connection)


def drop_resource_parent_guards(connection) -> None:
    """Remove our barriers during atomic parent rebuild; restore before commit.

    Turso validates triggers during RENAME, including triggers on other tables
    that reference a temporarily absent parent. Transaction rollback restores
    the old guards if the rebuild fails.
    """
    if connection.dialect.driver != "aioturso":
        return
    quote = connection.dialect.identifier_preparer.quote
    for name, _table, _column, _parent in _guard_specs():
        for operation in ("insert", "update"):
            connection.exec_driver_sql(f"DROP TRIGGER IF EXISTS {quote(name + '_' + operation)}")
