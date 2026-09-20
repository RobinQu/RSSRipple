# ruff: noqa: E402
import asyncio
import os

from sqlalchemy.engine import make_url

url = make_url(os.environ["DATABASE_URL"])
assert url.drivername == "postgresql+asyncpg" and url.host == "127.0.0.1"
assert (url.username, url.password, url.database) == ("organize_test",) * 3
import json
import uuid
from pathlib import Path
from unittest.mock import patch

import app.services.work_deletion as deletion
from sqlalchemy import text, update
from sqlalchemy.exc import DBAPIError

import app.database as database
import app.models  # noqa: F401
from app.api.v1.movies import delete_movie
from app.api.v1.series import delete_series
from app.models.channel import Channel
from app.models.file_resource import FileResource
from app.models.movie import Movie
from app.models.resource_file_assignment import ResourceFileAssignment
from app.models.resource_work_link import ResourceWorkLink
from app.models.series import TVSeries
from app.models.work_collection import WorkCollection


async def case(kind, model, edit_first):
    async with database.async_session_factory() as db:
        channel = Channel(name="Synthetic race", url="https://example.invalid/" + str(uuid.uuid4()), field_mapping={})
        collection = WorkCollection(title_cn="Synthetic update race")
        db.add(collection)
        await db.flush()
        work = (
            Movie(title_cn="Synthetic manual race")
            if kind == "movie"
            else TVSeries(title_cn="Synthetic", collection_id=collection.id, season_number=1)
        )
        db.add_all([channel, work])
        await db.flush()
        resource = FileResource(
            channel_id=channel.id,
            guid=str(uuid.uuid4()),
            title_raw="Synthetic",
            torrent_url="magnet:?xt=urn:btih:synthetic",
        )
        db.add(resource)
        await db.flush()
        args = {"resource_id": resource.id, kind + "_id": work.id, "source": "auto"}
        if model is ResourceFileAssignment:
            args["file_path"] = "synthetic.mkv"
        row = model(**args)
        db.add(row)
        await db.commit()
        wid, rid = work.id, row.id
    endpoint = delete_movie if kind == "movie" else delete_series
    async with database.async_session_factory() as editor, database.async_session_factory() as deleter:
        if edit_first:
            await editor.execute(update(model).where(model.id == rid).values(source="manual"))
            response = await asyncio.wait_for(endpoint(wid, deleter), 5)
            assert response.status_code == 409
            assert json.loads(response.body)["error"]["code"] == "INVALID_STATE"
            await editor.commit()
            response = await endpoint(wid, deleter)
            assert response.status_code == 409
            assert json.loads(response.body)["error"]["code"] == "DELETE_BLOCKED"
        else:
            checked, release = asyncio.Event(), asyncio.Event()
            real_check = deletion.manual_deletion_references

            async def paused(*args):
                refs = await real_check(*args)
                checked.set()
                await asyncio.wait_for(release.wait(), 5)
                return refs

            with patch.object(deletion, "manual_deletion_references", paused):
                task = asyncio.create_task(endpoint(wid, deleter))
                await asyncio.wait_for(checked.wait(), 5)
                await editor.execute(text("SET LOCAL lock_timeout = '300ms'"))
                try:
                    await editor.execute(update(model).where(model.id == rid).values(source="manual"))
                except DBAPIError as exc:
                    assert getattr(exc.orig, "sqlstate", None) == "55P03"
                    await editor.rollback()
                else:
                    raise AssertionError("manual update bypassed delete lock")
                release.set()
                await asyncio.wait_for(task, 5)
    async with database.async_session_factory() as db:
        row = await db.get(model, rid)
        work = await db.get(Movie if kind == "movie" else TVSeries, wid)
        assert (work is not None) == edit_first
        if edit_first:
            assert row is not None and row.source == "manual" and getattr(row, kind + "_id") == wid
        elif model is ResourceFileAssignment:
            assert row is not None and row.source == "auto" and getattr(row, kind + "_id") is None
        else:
            assert row is None
        return {"kind": kind, "reference": model.__tablename__, "edit_first": edit_first, "passed": True}


async def main():
    async with database.engine.begin() as conn:
        await conn.run_sync(database.Base.metadata.create_all)
    results = [
        await case(kind, model, order)
        for kind in ("series", "movie")
        for model in (ResourceWorkLink, ResourceFileAssignment)
        for order in (True, False)
    ]
    Path("/tmp/rssripple-v12-updates-hv.json").write_text(json.dumps(results, indent=2) + "\n")
    print(results)


asyncio.run(main())
