"""Actual metadata upserts; recorded labels and synthetic source alias history."""
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.models.audio_work import AudioWork
from app.models.movie import Movie
from app.models.series import TVSeries
from app.models.work_collection import WorkCollection
from app.services import metadata_service as ms
from app.services.text_normalizer import normalize_title
from tests.integration.search_text.test_storage import input_values

CASES = [(Movie, 'new'), (Movie, 'update'), (TVSeries, 'new'),
         (TVSeries, 'update'), (AudioWork, 'update')]


async def check_metadata(pair, monkeypatch, model, mode):
    engine, factory = pair
    monkeypatch.setattr(settings, 'database_url', engine.url.render_as_string(hide_password=False))
    poster = AsyncMock(return_value=None)
    monkeypatch.setattr(ms, 'download_and_cache_poster', poster)
    title, _, _ = input_values(model, 4095)
    aliases = [f'Synthetic source alias {n:02d} ' + 'a' * 330 for n in range(14)]
    aliases[-1] += ' tailmarker'
    assert all(len(alias) <= 512 for alias in aliases)
    assert sum(map(len, aliases)) > 4096
    data = {'title_cn': title, 'external_source': 'llm_search',
            'external_id': 'synthetic-search-length',
            'content_type': {Movie: 'movie', TVSeries: 'tv', AudioWork: 'music'}[model]}

    async def upsert(db, payload):
        if model is TVSeries:
            return await ms.create_or_update_series_from_external(db, payload, season_hint=1)
        if model is Movie:
            return await ms.create_or_update_movie_from_external(db, payload)
        return await ms.create_or_update_audio_work_from_external(db, payload)

    original = None
    if mode == 'update':
        async with factory() as db:
            row = await upsert(db, data)
            assert row is not None
            await db.commit()
            original = row.id
    # Audio takes incoming titles as aliases during refresh. It has no
    # alt_titles ingestion, so exercise a real history of bounded title inputs.
    incoming = ([dict(data, title_cn=alias) for alias in aliases] if model is AudioWork
                else [dict(data, alt_titles=aliases)])
    for payload in incoming:
        async with factory() as db:
            row = await upsert(db, payload)
            assert row is not None
            if original is not None:
                assert row.id == original
            await db.commit()
            identity = row.id
    async with factory() as db:
        stored = await db.get(model, identity)
        assert set(aliases) <= set(stored.aliases)
        assert len(stored.search_text) > 4096
        for alias in aliases:
            assert normalize_title(alias) in stored.search_text
        assert 'tailmarker' in stored.search_text
        if model is TVSeries:
            assert stored.season_number == 1 and stored.collection_id
            collection = await db.get(WorkCollection, stored.collection_id)
            assert collection.search_text
            if mode == 'new':
                assert set(aliases) <= set(collection.aliases)
                assert len(collection.search_text) > 4096
    assert all(call.args == (None,) for call in poster.await_args_list)


@pytest.mark.parametrize('model,mode', CASES)
async def test_postgres_metadata(dedup_postgres, monkeypatch, model, mode):
    await check_metadata(dedup_postgres, monkeypatch, model, mode)


@pytest.mark.parametrize('model,mode', CASES)
async def test_turso_metadata(dedup_turso, monkeypatch, model, mode):
    await check_metadata(dedup_turso, monkeypatch, model, mode)
