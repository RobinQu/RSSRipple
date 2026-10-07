"""Restore legacy FK semantics without changing application associations."""

from collections import defaultdict

from sqlalchemy import inspect, text

# Only columns introduced by the lightweight migration are in scope. Targets
# and delete actions come from ORM metadata, not a second schema definition.
_COLUMNS = (
    ("file_resources", "audio_work_id"),
    ("tv_series", "collection_id"),
    ("movies", "collection_id"),
    ("downloader_instances", "volume_id"),
    ("libraries", "media_server_id"),
    ("libraries", "volume_id"),
    ("file_resources", "collection_id"),
)


async def repair_turso_foreign_keys(engine) -> None:
    from app.database import Base

    async with engine.begin() as conn:
        # DDL is forbidden after BEGIN CONCURRENT. This startup-only phase
        # must own a fresh ordinary transaction, including every table swap.
        await conn.execute(text("BEGIN"))
        quote = conn.dialect.identifier_preparer.quote
        missing = defaultdict(list)
        catalogs = {}
        for table, column in _COLUMNS:
            if table not in catalogs:
                catalogs[table] = (await conn.execute(text(f"PRAGMA foreign_key_list({quote(table)})"))).all()
            (fk,) = Base.metadata.tables[table].c[column].foreign_keys
            target, target_column = fk.column.table.name, fk.column.name
            action = fk.ondelete or "NO ACTION"
            existing = [row for row in catalogs[table] if row[3] == column]
            if existing:
                if not all((row[2], row[4], row[6]) == (target, target_column, action) for row in existing):
                    raise RuntimeError(f"Unexpected foreign key semantics for {table}.{column}")
                continue
            orphan_ids = list(
                (
                    await conn.execute(
                        text(
                            f"SELECT child.id FROM {quote(table)} child LEFT JOIN {quote(target)} parent "
                            f"ON child.{quote(column)}=parent.{quote(target_column)} "
                            f"WHERE child.{quote(column)} IS NOT NULL "
                            f"AND parent.{quote(target_column)} IS NULL LIMIT 10"
                        )
                    )
                ).scalars()
            )
            if orphan_ids:
                raise RuntimeError(f"Cannot add {table}.{column} foreign key; orphan row IDs: {orphan_ids}")
            missing[table].append(
                f"FOREIGN KEY ({quote(column)}) REFERENCES {quote(target)} ({quote(target_column)}) ON DELETE {action}"
            )
        if not missing:
            return
        await conn.execute(text("PRAGMA foreign_keys=OFF"))
        if await conn.scalar(text("PRAGMA foreign_keys")) != 0:
            raise RuntimeError("Cannot suspend foreign keys for atomic schema repair")
        try:
            if {"file_resources", "movies", "tv_series"}.intersection(missing):
                from app.services.resource_parent_guard import drop_resource_parent_guards

                await conn.run_sync(drop_resource_parent_guards)
            for table, clauses in missing.items():
                ddl = await conn.scalar(
                    text("SELECT sql FROM sqlite_master WHERE type='table' AND name=:name"), {"name": table}
                )
                objects = list(
                    (
                        await conn.execute(
                            text(
                                "SELECT sql FROM sqlite_master WHERE tbl_name=:name AND type IN ('index','trigger') "
                                "AND sql IS NOT NULL ORDER BY type,name"
                            ),
                            {"name": table},
                        )
                    ).scalars()
                )
                columns = [row[1] for row in (await conn.execute(text(f"PRAGMA table_info({quote(table)})"))).all()]
                temporary = f"__fk_repair_{table}"
                start, end = ddl.index("("), ddl.rindex(")")
                rebuilt = f"CREATE TABLE {quote(temporary)} " + ddl[start:end] + ", " + ", ".join(clauses) + ddl[end:]
                await conn.execute(text(rebuilt))
                column_list = ", ".join(quote(column) for column in columns)
                await conn.execute(
                    text(f"INSERT INTO {quote(temporary)} ({column_list}) SELECT {column_list} FROM {quote(table)}")
                )
                await conn.execute(text(f"DROP TABLE {quote(table)}"))
                await conn.execute(text(f"ALTER TABLE {quote(temporary)} RENAME TO {quote(table)}"))
                for statement in objects:
                    await conn.execute(text(statement))
            if {"file_resources", "movies", "tv_series"}.intersection(missing):
                from app.services.resource_parent_guard import ensure_resource_parent_guards

                await conn.run_sync(ensure_resource_parent_guards)
        finally:
            await conn.execute(text("PRAGMA foreign_keys=ON"))


async def repair_postgres_foreign_keys(conn) -> None:
    """Run under startup's transaction-scoped advisory lock and DDL timeout."""
    from app.database import Base

    catalogs = await conn.run_sync(lambda sync: {
        table: inspect(sync).get_foreign_keys(table) for table in dict.fromkeys(t for t, _ in _COLUMNS)
    })
    quote = conn.dialect.identifier_preparer.quote
    for table, column in _COLUMNS:
        (fk,) = Base.metadata.tables[table].c[column].foreign_keys
        target, target_column = fk.column.table.name, fk.column.name
        action = fk.ondelete or "NO ACTION"
        existing = [item for item in catalogs[table] if item["constrained_columns"] == [column]]
        if existing:
            if not all(
                item["referred_table"] == target
                and item["referred_columns"] == [target_column]
                and item["options"].get("ondelete", "NO ACTION") == action
                for item in existing
            ):
                raise RuntimeError(f"Unexpected foreign key semantics for {table}.{column}")
            continue
        orphan_ids = list((await conn.execute(text(
            f"SELECT child.id FROM {quote(table)} child LEFT JOIN {quote(target)} parent "
            f"ON child.{quote(column)}=parent.{quote(target_column)} "
            f"WHERE child.{quote(column)} IS NOT NULL "
            f"AND parent.{quote(target_column)} IS NULL LIMIT 10"
        ))).scalars())
        if orphan_ids:
            raise RuntimeError(f"Cannot add {table}.{column} foreign key; orphan row IDs: {orphan_ids}")
        # ALTER validates every existing row again under PostgreSQL's table
        # lock, so a writer cannot slip an orphan past the diagnostic precheck.
        name = f"fk_repair_{table}_{column}"
        await conn.execute(text(
            f"ALTER TABLE {quote(table)} ADD CONSTRAINT {quote(name)} "
            f"FOREIGN KEY ({quote(column)}) REFERENCES {quote(target)} ({quote(target_column)}) ON DELETE {action}"
        ))
