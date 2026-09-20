"""Read-only preflight must export all affected identities before startup."""

import json

import pytest
from sqlalchemy import event, text

from scripts.verify_upgrade_foreign_keys import export_orphans


@pytest.mark.parametrize("mode", ["clean", "orphans", "missing-target", "missing-column"])
async def test_preflight_is_read_only_and_complete(db_engine, tmp_path, mode):
    # Deliberately small legacy schema: the report must not run create_all.
    async with db_engine.begin() as conn:
        await conn.execute(text("DROP TABLE movies"))
        columns = "id TEXT PRIMARY KEY" + (", collection_id TEXT" if mode != "missing-column" else "")
        await conn.execute(text(f"CREATE TABLE movies ({columns})"))
        if mode == "missing-target":
            await conn.execute(text("DROP TABLE work_collections"))
        if mode in {"orphans", "missing-target"}:
            await conn.execute(
                text("INSERT INTO movies(id,collection_id) VALUES (:id,:parent)"),
                [{"id": f"synthetic-{n:03}", "parent": f"missing-{n:03}"} for n in range(103)],
            )
        before = (await conn.execute(text("SELECT * FROM movies ORDER BY id"))).all()
    statements = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement.strip().upper())

    event.listen(db_engine.sync_engine, "before_cursor_execute", capture)
    output = tmp_path / "orphans.jsonl"
    try:
        code = await export_orphans(str(output))
    finally:
        event.remove(db_engine.sync_engine, "before_cursor_execute", capture)
    assert statements and all(s.startswith(("SELECT", "PRAGMA")) for s in statements), statements
    rows = [json.loads(line) for line in output.read_text().splitlines()]
    if mode in {"orphans", "missing-target"}:
        assert code == 1 and len(rows) == 103
        assert rows == [
            {
                "table": "movies",
                "column": "collection_id",
                "id": f"synthetic-{n:03}",
                "parent_id": f"missing-{n:03}",
                "target": "work_collections.id",
            }
            for n in range(103)
        ]
    else:
        assert code == 0 and rows == []
    async with db_engine.connect() as conn:
        assert (await conn.execute(text("SELECT * FROM movies ORDER BY id"))).all() == before
