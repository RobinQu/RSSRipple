"""Real legacy Turso DDL: no ORM creation of the new constraints."""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DatabaseError, IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.services.decision_schema import install_decision_constraints


@pytest.fixture
async def db_session(tmp_path):
    # Upgrade DDL must own a fresh ordinary transaction, never BEGIN CONCURRENT.
    engine = create_async_engine(f"sqlite+aioturso:///{tmp_path / 'upgrade.db'}")
    try:
        async with async_sessionmaker(engine)() as session:
            async with session.begin():
                await session.execute(text("BEGIN"))
                yield session
    finally:
        await engine.dispose()


async def legacy(db):
    await db.execute(text("DROP TABLE IF EXISTS pending_decisions"))
    await db.execute(
        text(
            "CREATE TABLE pending_decisions "
            "(id TEXT PRIMARY KEY,agent_id TEXT,status TEXT,candidates TEXT,legacy_note TEXT)"
        )
    )
    return await db.connection()


async def test_historical_rows_survive_and_both_write_guards_apply(db_session):
    conn = await legacy(db_session)
    await conn.execute(text("INSERT INTO pending_decisions VALUES ('history','agent','decided','[]','keep')"))
    await install_decision_constraints(conn)
    await install_decision_constraints(conn)
    original = (await conn.execute(text("SELECT status,legacy_note,decision_key FROM pending_decisions"))).one()
    assert tuple(original) == ("decided", "keep", None)
    for statement in [
        "INSERT INTO pending_decisions(id,agent_id,status) VALUES ('bad','agent','pending')",
        "UPDATE pending_decisions SET status='pending' WHERE id='history'",
    ]:
        with pytest.raises(DatabaseError, match="pending decision requires identity key"):
            async with db_session.begin_nested():
                await conn.execute(text(statement))
    await conn.execute(
        text(
            "INSERT INTO pending_decisions(id,agent_id,status,decision_key) "
            "VALUES ('one','agent','pending','synthetic-key')"
        )
    )
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await conn.execute(
                text(
                    "INSERT INTO pending_decisions(id,agent_id,status,decision_key) "
                    "VALUES ('two','agent','pending','synthetic-key')"
                )
            )
    await conn.execute(text("UPDATE pending_decisions SET status='decided' WHERE id='one'"))
    await conn.execute(
        text(
            "INSERT INTO pending_decisions(id,agent_id,status,decision_key) "
            "VALUES ('two','agent','pending','synthetic-key')"
        )
    )


async def test_unreviewed_pending_fails_before_any_schema_or_data_changes(db_session):
    conn = await legacy(db_session)
    await conn.execute(text("INSERT INTO pending_decisions VALUES ('unreviewed','agent','pending','[]','keep')"))
    with pytest.raises(ValueError, match="unreviewed"):
        await install_decision_constraints(conn)
    assert "decision_key" not in [row[1] for row in await conn.execute(text("PRAGMA table_info(pending_decisions)"))]
    assert (await conn.execute(text("SELECT * FROM pending_decisions"))).one() == (
        "unreviewed",
        "agent",
        "pending",
        "[]",
        "keep",
    )


@pytest.mark.parametrize(
    "bad_ddl",
    [
        "CREATE INDEX uq_pending_decisions_agent_key ON pending_decisions(agent_id,decision_key)",
        "CREATE UNIQUE INDEX uq_pending_decisions_agent_key ON pending_decisions(agent_id,decision_key) WHERE status='decided'",
        "CREATE TRIGGER ck_pending_decision_key_insert BEFORE INSERT ON pending_decisions BEGIN SELECT 1; END",
    ],
)
async def test_wrong_named_guard_is_rejected_before_schema_mutation(db_session, bad_ddl):
    conn = await legacy(db_session)
    await conn.execute(text("ALTER TABLE pending_decisions ADD COLUMN decision_key TEXT"))
    await conn.execute(text(bad_ddl))
    before = list(await conn.execute(text("SELECT name,sql FROM sqlite_master ORDER BY name")))
    with pytest.raises(ValueError, match="Unexpected"):
        await install_decision_constraints(conn)
    after = list(await conn.execute(text("SELECT name,sql FROM sqlite_master ORDER BY name")))
    assert after == before


async def test_upgrade_failure_rolls_back_columns_guards_and_data(tmp_path):
    engine = create_async_engine(f"sqlite+aioturso:///{tmp_path / 'rollback.db'}")
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "CREATE TABLE pending_decisions "
                    "(id TEXT PRIMARY KEY,agent_id TEXT,status TEXT,candidates TEXT,legacy_note TEXT)"
                )
            )
            await conn.execute(text("INSERT INTO pending_decisions VALUES ('history','agent','decided','[]','keep')"))
        with pytest.raises(RuntimeError, match="after guards"):
            async with engine.begin() as conn:
                await conn.execute(text("BEGIN"))
                await install_decision_constraints(conn)
                raise RuntimeError("after guards")
        async with engine.connect() as conn:
            columns = [row[1] for row in await conn.execute(text("PRAGMA table_info(pending_decisions)"))]
            assert "decision_key" not in columns
            assert not list(
                await conn.execute(
                    text(
                        "SELECT name FROM sqlite_master WHERE name LIKE 'ck_pending%' "
                        "OR name='uq_pending_decisions_agent_key'"
                    )
                )
            )
            assert (await conn.execute(text("SELECT legacy_note FROM pending_decisions"))).scalar_one() == "keep"
    finally:
        await engine.dispose()


async def test_nonempty_but_inconsistent_key_scope_requires_review(db_session):
    conn = await legacy(db_session)
    await conn.execute(text("ALTER TABLE pending_decisions ADD COLUMN decision_key TEXT"))
    await conn.execute(text("ALTER TABLE pending_decisions ADD COLUMN decision_scope JSON"))
    await conn.execute(
        text(
            "INSERT INTO pending_decisions(id,agent_id,status,decision_key,decision_scope) "
            "VALUES ('corrupt','agent','pending','v1:wrong',:scope)"
        ),
        {"scope": '{"version":1,"kind":"movie","work_id":"movie","season":null,"episode":null}'},
    )
    with pytest.raises(ValueError, match="corrupt"):
        await install_decision_constraints(conn)
    assert not list(
        await conn.execute(text("SELECT name FROM sqlite_master WHERE name='uq_pending_decisions_agent_key'"))
    )
