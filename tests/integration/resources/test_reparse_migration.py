"""Real upgrade entry point on both supported engines and durable request constraints."""

import uuid

import pytest
from sqlalchemy import delete, text
from sqlalchemy.exc import IntegrityError

from app import database
from app.config import settings
from app.models.channel import Channel
from app.models.file_resource import FileResource
from app.models.resource_reparse_request import ResourceReparseRequest
from app.services.resource_reparse_requests import create_request, finish_request
from app.utils.time import utcnow


async def _upgrade_contract(db_pair, monkeypatch, record_property):
    engine, factory = db_pair
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(settings, "database_url", engine.url.render_as_string(hide_password=False))
    rid, marker = str(uuid.uuid4()), utcnow()
    async with factory() as db:
        channel = Channel(name="Synthetic legacy marker", type="rss_feed", url="https://example.invalid", field_mapping={})
        db.add(channel)
        await db.flush()
        channel_id = channel.id
        db.add(FileResource(id=rid, channel_id=channel_id, guid=rid, title_raw="Synthetic legacy",
                            torrent_url="magnet:?xt=urn:btih:synthetic", confirmation_ignored_at=marker))
        await db.commit()
    # Represent the previous schema by removing only this new table, then
    # invoke the production migration entry point twice (not create_all alone).
    async with engine.begin() as conn:
        await conn.run_sync(ResourceReparseRequest.__table__.drop)
    await database.create_tables()
    async with factory() as db:
        assert (await db.get(FileResource, rid)).confirmation_ignored_at == marker
        request = await create_request(db, rid, channel_id)
        await db.commit()
    await database.create_tables()
    async with factory() as db:
        assert await db.get(ResourceReparseRequest, request.id)
        report_sql = text(
            "SELECT r.id, r.channel_id, r.title_raw, r.confirmation_ignored_at, "
            "q.id AS pending_request_id, q.error_message AS delivery_error "
            "FROM file_resources r LEFT JOIN resource_reparse_requests q ON q.resource_id=r.id "
            "WHERE r.confirmation_ignored_at IS NOT NULL AND r.id > :after_id ORDER BY r.id LIMIT 100"
        )
        rows = (await db.execute(report_sql, {"after_id": ""})).mappings().all()
        assert len(rows) == 1 and rows[0]["id"] == rid and rows[0]["pending_request_id"] == request.id
        assert (await db.execute(report_sql, {"after_id": rid})).all() == []
        assert await create_request(db, rid, channel_id) is None
        assert (await db.get(FileResource, rid)).confirmation_ignored_at == marker
        await db.rollback()
    async with factory() as db:
        db.add(ResourceReparseRequest(resource_id=rid, next_attempt_at=utcnow()))
        with pytest.raises(IntegrityError):
            await db.flush()
        await db.rollback()
    async with factory() as db:
        assert await db.get(ResourceReparseRequest, request.id)
        await finish_request(db, request.id, rid)
        await db.commit()
        aborted = await create_request(db, rid, channel_id)
        await db.rollback()
    async with factory() as db:
        assert await db.get(ResourceReparseRequest, aborted.id) is None
        retained = await create_request(db, rid, channel_id)
        await db.commit()
        await db.execute(delete(FileResource).where(FileResource.id == rid))
        await db.commit()
    async with factory() as db:
        assert await db.get(ResourceReparseRequest, retained.id) is None
    record_property("backend", engine.dialect.name)
    record_property("production_migration_reruns", 2)
    record_property("legacy_manual_marker_preserved", True)
    record_property("unique_constraint_and_cascade", True)
    record_property("request_transaction_rollback", True)
    record_property("legacy_report_cursor_checked", True)


async def test_postgres_reparse_upgrade(reparse_database, monkeypatch, record_property):
    await _upgrade_contract(reparse_database, monkeypatch, record_property)


async def test_turso_reparse_upgrade(reparse_turso_database, monkeypatch, record_property):
    await _upgrade_contract(reparse_turso_database, monkeypatch, record_property)
