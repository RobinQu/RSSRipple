"""Recorded labels, synthetic alias sizes, actual DB/FTS writes and reads."""
import hashlib
import json
from pathlib import Path

import pytest
from sqlalchemy import select

from app.config import settings
from app.models.audio_work import AudioWork
from app.models.movie import Movie
from app.models.series import TVSeries
from app.models.work_collection import WorkCollection
from app.services import fts
from app.services.text_normalizer import normalize_title

MODELS = [Movie, TVSeries, AudioWork, WorkCollection]
LENGTHS = [4095, 4096, 4097, 8192, 'unicode']


def input_values(model, length):
    raw = (Path(__file__).parents[2] / 'fixtures/prod_works_v1.json').read_bytes()
    assert hashlib.sha256(raw).hexdigest() == 'd11651d2162ced23e8d919af0bff2d9f316e203234cc854909ba5f444a35ec32'
    rows = json.loads(raw)['tables']
    # Audio has no captured rows; borrow a movie label, not its identity/type.
    title = (rows.get(model.__tablename__) or rows['movies'])[0]['title_cn']
    prefix = normalize_title(title) + ' '
    if length == 'unicode':
        alias = '\ufb03' * 1500 + ' tailmarker'
        expected = prefix + 'ffi' * 1500 + ' tailmarker'
        assert len(alias) < 4096 < len(expected)
    else:
        alias = 'a' * (length - len(prefix) - len(' tailmarker')) + ' tailmarker'
        expected = prefix + alias
        assert len(expected) == length
    return title, alias, expected


async def seed(db, model, title, aliases):
    row = model(title_cn=title, aliases=aliases)
    if model is TVSeries:
        parent = WorkCollection(title_cn=title)
        db.add(parent)
        await db.flush()
        row.collection_id = parent.id
    db.add(row)
    await db.commit()
    return row.id


async def check_storage(pair, monkeypatch, model, length, operation):
    engine, factory = pair
    monkeypatch.setattr(settings, 'database_url', engine.url.render_as_string(hide_password=False))
    if engine.dialect.name == 'sqlite':
        await fts.ensure_fts_tables()
    title, alias, expected = input_values(model, length)
    async with factory() as db:
        identity = await seed(db, model, title, [alias] if operation == 'insert' else ['short'])
    if operation == 'update':
        async with factory() as db:
            row = await db.get(model, identity)
            row.aliases = [alias]
            await db.commit()
    async with factory() as db:
        row = await db.get(model, identity)
        assert row.aliases == [alias]
        assert row.search_text == expected
        if model is WorkCollection:
            # Collections use the normalized base column, not the work sidecar.
            ids = list(await db.scalars(select(model.id).where(model.search_text.contains('tailmarker'))))
        else:
            search = {Movie: fts.search_movie_fts, TVSeries: fts.search_series_fts,
                      AudioWork: fts.search_audio_work_fts}[model]
            ids = await search(db, 'tailmarker')
        assert ids == [identity]


@pytest.mark.parametrize('model', MODELS)
@pytest.mark.parametrize('length', LENGTHS)
@pytest.mark.parametrize('operation', ['insert', 'update'])
async def test_postgres_storage(dedup_postgres, monkeypatch, model, length, operation):
    await check_storage(dedup_postgres, monkeypatch, model, length, operation)


@pytest.mark.parametrize('model', MODELS)
@pytest.mark.parametrize('length', LENGTHS)
@pytest.mark.parametrize('operation', ['insert', 'update'])
async def test_turso_storage(dedup_turso, monkeypatch, model, length, operation):
    await check_storage(dedup_turso, monkeypatch, model, length, operation)
