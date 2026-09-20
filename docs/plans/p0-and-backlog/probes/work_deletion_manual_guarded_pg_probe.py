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

from sqlalchemy import update

import app.database as database
import app.models  # noqa: F401
from app.api.v1.movies import delete_movie
from app.models.channel import Channel
from app.models.file_resource import FileResource
from app.models.movie import Movie
from app.models.resource_file_assignment import ResourceFileAssignment
from app.models.resource_work_link import ResourceWorkLink


async def case(model):
    async with database.async_session_factory() as db:
        channel = Channel(name="Synthetic race", url="https://example.invalid/" + str(uuid.uuid4()), field_mapping={})
        work = Movie(title_cn="Synthetic manual race")
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
        args = {"resource_id": resource.id, "movie_id": work.id, "source": "auto"}
        if model is ResourceFileAssignment:
            args["file_path"] = "synthetic.mkv"
        row = model(**args)
        db.add(row)
        await db.commit()
        wid, rid = work.id, row.id
    async with database.async_session_factory() as editor, database.async_session_factory() as deleter:
        await editor.execute(update(model).where(model.id == rid).values(source="manual"))
        response = await asyncio.wait_for(delete_movie(wid, deleter), 5)
        assert response.status_code == 409
        assert json.loads(response.body)["error"]["code"] == "INVALID_STATE"
        await editor.commit()
        response = await delete_movie(wid, deleter)
        assert response.status_code == 409
        assert json.loads(response.body)["error"]["code"] == "DELETE_BLOCKED"
    async with database.async_session_factory() as db:
        row = await db.get(model, rid)
        work = await db.get(Movie, wid)
        return {
            "reference": model.__tablename__,
            "work_retained": work is not None,
            "manual_target_retained": row is not None and row.movie_id == wid,
        }


async def main():
    async with database.engine.begin() as conn:
        await conn.run_sync(database.Base.metadata.create_all)
    results = [await case(model) for model in (ResourceWorkLink, ResourceFileAssignment)]
    Path("/tmp/rssripple-v12-manual-gx.json").write_text(json.dumps(results, indent=2) + "\n")
    print(results)
    assert all(row["work_retained"] and row["manual_target_retained"] for row in results)


asyncio.run(main())
