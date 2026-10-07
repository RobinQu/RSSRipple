"""Dedup must re-read committed edits rather than deleting a stale ORM row."""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import DBAPIError

from app.database import _is_retryable_lock_error
from app.models.movie import Movie
from app.models.series import TVSeries
from app.models.work_collection import WorkCollection
from app.services.metadata_dedup import DedupReport, _merge_movie_group, _merge_series_group


@pytest.mark.parametrize("kind", ["movie", "series"])
async def test_edit_committed_after_dedup_read_is_preserved(dedup_postgres, kind):
    await _committed_edit(dedup_postgres, kind, expect_conflict=False)


@pytest.mark.parametrize("kind", ["movie", "series"])
async def test_turso_stale_merge_retries_current_manual_values(dedup_turso, kind):
    await _committed_edit(dedup_turso, kind, expect_conflict=True)


async def _committed_edit(database, kind, *, expect_conflict):
    _, factory = database
    model = Movie if kind == "movie" else TVSeries
    async with factory() as seed:
        transaction_anchor = WorkCollection(title_cn="合成事务锚点")
        seed.add(transaction_anchor)
        rows = []
        for offset in range(2):
            attrs = dict(title_en="Synthetic concurrent edit", description="old automatic value",
                         created_at=datetime(2026, 1, 1) + timedelta(days=offset))
            if kind == "series":
                collection = WorkCollection(title_cn="合成并发合集")
                seed.add(collection)
                await seed.flush()
                attrs.update(collection_id=collection.id, season_number=1)
            rows.append(model(**attrs))
        seed.add_all(rows)
        await seed.commit()
        old_id, new_id = [row.id for row in rows]
    async with factory() as merge_db:
        if expect_conflict:
            # Turso defers native BEGIN until the first write. Establish a
            # genuine MVCC snapshot using an unrelated row before reading.
            await merge_db.execute(update(WorkCollection).where(
                WorkCollection.id == transaction_anchor.id,
            ).values(title_cn="合成已开启事务"))
        older = await merge_db.get(model, old_id)
        newer = await merge_db.get(model, new_id)
        assert newer.manually_edited_fields is None
        async with factory() as edit_db:
            edited = await edit_db.get(model, new_id)
            edited.description = "new manual correction"
            edited.manually_edited_fields = ["description"]
            await edit_db.commit()
        merge = _merge_movie_group if kind == "movie" else _merge_series_group
        conflicted = False
        try:
            await merge(merge_db, [older, newer], DedupReport(), survivor=older)
            await merge_db.commit()
        except DBAPIError as exc:
            assert _is_retryable_lock_error(exc)
            conflicted = True
            await merge_db.rollback()
    assert conflicted == expect_conflict
    if conflicted:
        async with factory() as retry:
            older = await retry.get(model, old_id)
            newer = await retry.get(model, new_id)
            assert newer.description == "new manual correction"
            await merge(retry, [older, newer], DedupReport(), survivor=older)
            await retry.commit()
    async with factory() as check:
        result = (await check.execute(select(model))).scalar_one()
        assert result.description == "new manual correction"
        assert "description" in result.manually_edited_fields


async def test_postgres_busy_editor_rolls_back_merge_before_retry(dedup_postgres):
    _, factory = dedup_postgres
    async with factory() as seed:
        rows = [Movie(title_en="Synthetic busy editor", description="automatic") for _ in range(2)]
        seed.add_all(rows)
        await seed.commit()
        ids = [row.id for row in rows]
    async with factory() as merger, factory() as editor:
        target, source = [await merger.get(Movie, identity) for identity in ids]
        editing = await editor.get(Movie, ids[1])
        editing.description = "manual before merge"
        editing.manually_edited_fields = ["description"]
        await editor.flush()  # Keep the real PostgreSQL row lock until commit.
        with pytest.raises(DBAPIError) as failure:
            await _merge_movie_group(merger, [target, source], DedupReport(), survivor=target)
        assert _is_retryable_lock_error(failure.value)
        await merger.rollback()
        await editor.commit()
    async with factory() as retry:
        target, source = [await retry.get(Movie, identity) for identity in ids]
        await _merge_movie_group(retry, [target, source], DedupReport(), survivor=target)
        await retry.commit()
    async with factory() as check:
        row = (await check.execute(select(Movie))).scalar_one()
        assert row.description == "manual before merge"
        assert row.manually_edited_fields == ["description"]
