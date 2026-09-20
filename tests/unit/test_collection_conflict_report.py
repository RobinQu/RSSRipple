"""A startup-independent conflict report must preserve all legacy data."""

import json

import pytest
from sqlalchemy import event, select, text

from app.models.series import TVSeries
from app.models.work_collection import WorkCollection
from scripts.verify_season_split import _write_collection_conflicts


@pytest.mark.parametrize("conflicts", [False, True])
async def test_conflict_report_is_complete_and_read_only(db_engine, db_session, tmp_path, conflicts):
    async with db_engine.begin() as conn:
        await conn.execute(text("DROP INDEX uq_tv_series_collection_season"))
    parent = WorkCollection(title_cn="Synthetic report parent")
    other = WorkCollection(title_cn="Synthetic control parent")
    db_session.add_all([parent, other])
    await db_session.flush()
    members = [
        TVSeries(
            title_cn=f"Synthetic protected {i}",
            collection_id=parent.id,
            season_number=1 if conflicts else i,
            manually_edited_fields=["title_cn"],
        )
        for i in range(103)
    ]
    control = TVSeries(title_cn="Synthetic control", collection_id=other.id, season_number=1)
    db_session.add_all([*members, control])
    await db_session.commit()
    expected = {member.id for member in members} if conflicts else set()
    before = {(r.id, r.collection_id, r.season_number, r.title_cn) for r in await db_session.scalars(select(TVSeries))}
    # Release the observer transaction before the independent report session.
    await db_session.rollback()
    statements = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(db_engine.sync_engine, "before_cursor_execute", record)
    report = tmp_path / "conflicts.jsonl"
    try:
        result = await _write_collection_conflicts(str(report))
    finally:
        event.remove(db_engine.sync_engine, "before_cursor_execute", record)
    assert result == int(conflicts)
    rows = [json.loads(line) for line in report.read_text().splitlines()]
    assert {row["id"] for row in rows} == expected
    assert all(row["work_count"] == 103 and row["manually_edited_fields"] == ["title_cn"] for row in rows)
    assert statements and all(sql.lstrip().upper().startswith("SELECT") for sql in statements)
    after = {(r.id, r.collection_id, r.season_number, r.title_cn) for r in await db_session.scalars(select(TVSeries))}
    assert after == before
