"""Prove data-preserving Turso rebuild primitives before migrating app tables."""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError


@pytest.mark.parametrize("mode", ["commit", "rollback", "orphan"])
async def test_rebuild_preserves_children_indexes_triggers_and_rollback(db_engine, mode):
    async with db_engine.begin() as conn:
        statements = [
            "CREATE TABLE probe_parent (id TEXT PRIMARY KEY)",
            "CREATE TABLE probe_target (id TEXT PRIMARY KEY, parent_id TEXT, legacy_extra TEXT)",
            "CREATE UNIQUE INDEX probe_extra ON probe_target(legacy_extra)",
            "CREATE TABLE probe_audit (target_id TEXT)",
            "CREATE TRIGGER probe_insert AFTER INSERT ON probe_target BEGIN INSERT INTO probe_audit VALUES (NEW.id); END",
        ]
        for action in ["CASCADE", "SET NULL", "RESTRICT"]:
            name = action.lower().replace(" ", "_")
            statements.append(
                f"CREATE TABLE probe_child_{name} (id TEXT PRIMARY KEY,target_id TEXT REFERENCES probe_target(id) ON DELETE {action})"
            )
        for statement in statements:
            await conn.execute(text(statement))
        await conn.execute(text("INSERT INTO probe_parent VALUES ('parent')"))
        await conn.execute(
            text("INSERT INTO probe_target VALUES (:id,:parent,:extra)"),
            {"id": "target", "parent": "missing" if mode == "orphan" else "parent", "extra": "retired-but-preserved"},
        )
        for name in ["cascade", "set_null", "restrict"]:
            await conn.execute(text(f"INSERT INTO probe_child_{name} VALUES ('child','target')"))

    async def rebuild():
        async with db_engine.begin() as conn:
            # The application's default BEGIN CONCURRENT cannot perform DDL.
            # A startup-only rebuild needs an explicit ordinary transaction.
            await conn.execute(text("BEGIN"))
            await conn.execute(text("UPDATE probe_target SET legacy_extra=legacy_extra"))
            await conn.execute(text("PRAGMA foreign_keys=OFF"))
            assert await conn.scalar(text("PRAGMA foreign_keys")) == 0
            try:
                await conn.execute(
                    text(
                        "CREATE TABLE probe_target_new (id TEXT PRIMARY KEY,parent_id TEXT REFERENCES probe_parent(id) ON DELETE SET NULL,legacy_extra TEXT)"
                    )
                )
                await conn.execute(text("INSERT INTO probe_target_new SELECT * FROM probe_target"))
                await conn.execute(text("DROP TABLE probe_target"))
                await conn.execute(text("ALTER TABLE probe_target_new RENAME TO probe_target"))
                await conn.execute(text("CREATE UNIQUE INDEX probe_extra ON probe_target(legacy_extra)"))
                await conn.execute(
                    text(
                        "CREATE TRIGGER probe_insert AFTER INSERT ON probe_target BEGIN INSERT INTO probe_audit VALUES (NEW.id); END"
                    )
                )
                # This Turso version does not return rows for foreign_key_check.
                # Validate the concrete relationship with an explicit anti-join.
                problems = (await conn.execute(text(
                    "SELECT child.id FROM probe_target child LEFT JOIN probe_parent parent "
                    "ON parent.id=child.parent_id WHERE child.parent_id IS NOT NULL AND parent.id IS NULL"
                ))).all()
                if problems:
                    raise ValueError("orphan precludes constraint validation")
                if mode == "rollback":
                    raise ValueError("injected after complete rebuild")
            finally:
                await conn.execute(text("PRAGMA foreign_keys=ON"))

    if mode == "commit":
        await rebuild()
    else:
        with pytest.raises(ValueError, match="orphan|injected"):
            await rebuild()
    async with db_engine.connect() as conn:
        assert await conn.scalar(text("PRAGMA foreign_keys")) == 1
        for name in ["cascade", "set_null", "restrict"]:
            assert (await conn.execute(text(f"SELECT * FROM probe_child_{name}"))).all() == [("child", "target")]
        assert (await conn.execute(text("SELECT * FROM probe_audit"))).all() == [("target",)]
        assert (await conn.execute(text("SELECT * FROM probe_target"))).all() == [
            ("target", "missing" if mode == "orphan" else "parent", "retired-but-preserved")
        ]
        foreign_keys = (await conn.execute(text("PRAGMA foreign_key_list('probe_target')"))).all()
        assert bool(foreign_keys) == (mode == "commit")
        objects = (
            (await conn.execute(text("SELECT name FROM sqlite_master WHERE name IN ('probe_extra','probe_insert')")))
            .scalars()
            .all()
        )
        assert set(objects) == {"probe_extra", "probe_insert"}
        assert await conn.scalar(text("SELECT count(*) FROM sqlite_master WHERE name='probe_target_new'")) == 0
    if mode == "commit":
        with pytest.raises(IntegrityError):
            async with db_engine.begin() as conn:
                await conn.execute(text("INSERT INTO probe_target VALUES ('invalid','missing','other')"))
        async with db_engine.begin() as conn:
            await conn.execute(text("INSERT INTO probe_target VALUES ('valid','parent','other')"))
        async with db_engine.connect() as conn:
            assert set((await conn.execute(text("SELECT target_id FROM probe_audit"))).scalars()) == {"target", "valid"}
