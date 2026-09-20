"""Probe actual Turso DDL support without touching application data."""

import asyncio
import json
import tempfile
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.exc import DatabaseError
from sqlalchemy.ext.asyncio import create_async_engine

from app.database import apply_db_pragmas, normalize_database_url


async def main():
    with tempfile.TemporaryDirectory(prefix="rssripple-v9-ddl-") as directory:
        engine = create_async_engine(normalize_database_url(f"sqlite+aioturso:///{directory}/probe.db"))
        apply_db_pragmas(engine)
        result = {}
        try:
            async with engine.begin() as conn:
                await conn.execute(text("CREATE TABLE parent (id TEXT PRIMARY KEY)"))
                await conn.execute(text("CREATE TABLE child (id TEXT PRIMARY KEY, parent_id TEXT)"))
                await conn.execute(text("PRAGMA journal_mode='mvcc'"))
                await conn.execute(text("INSERT INTO parent VALUES ('parent')"))
                await conn.execute(text("INSERT INTO child VALUES ('child','parent')"))
            try:
                async with engine.begin() as conn:
                    await conn.execute(
                        text(
                            "ALTER TABLE child ADD CONSTRAINT fk_child_parent "
                            "FOREIGN KEY (parent_id) REFERENCES parent(id) ON DELETE SET NULL"
                        )
                    )
                result["add_constraint_supported"] = True
            except DatabaseError as exc:
                result["add_constraint_supported"] = False
                result["error"] = str(exc.orig)
            async with engine.connect() as conn:
                result["foreign_keys_before"] = await conn.scalar(text("PRAGMA foreign_keys"))
                await conn.execute(text("UPDATE child SET parent_id=parent_id WHERE id='child'"))
                await conn.execute(text("PRAGMA foreign_keys=OFF"))
                result["foreign_keys_inside_transaction"] = await conn.scalar(text("PRAGMA foreign_keys"))
                await conn.rollback()
                result["child_preserved"] = (await conn.execute(text("SELECT * FROM child"))).all() == [
                    ("child", "parent")
                ]
            print(json.dumps(result, indent=2))
            Path("/tmp/rssripple-v9-turso-ddl-result.json").write_text(json.dumps(result, indent=2) + "\n")
        finally:
            await engine.dispose()


asyncio.run(main())
