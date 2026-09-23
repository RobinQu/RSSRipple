"""Concurrent manual title edit while an automatic refresh awaits a poster."""

import asyncio
import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

PROJECT = "rssripple-v14-metadata-nv"
[container] = json.loads(subprocess.check_output(["docker", "inspect", f"{PROJECT}-postgres-1"]))
assert container["Config"]["Labels"]["com.docker.compose.project"] == PROJECT
address = container["NetworkSettings"]["Networks"][PROJECT + "_isolated"]["IPAddress"]
os.environ["DATABASE_URL"] = f"postgresql+asyncpg://probe:probe@{address}:5432/probe"

from sqlalchemy import inspect, select  # noqa: E402

from app import database  # noqa: E402
from app.models.movie import Movie  # noqa: E402
from app.schemas.metadata_search import MetadataCandidate  # noqa: E402
from app.services.metadata_search import apply_work_metadata  # noqa: E402


async def main():
    try:
        async with database.engine.begin() as conn:
            assert not await conn.run_sync(lambda sync: inspect(sync).has_table("movies"))
            await conn.run_sync(database.Base.metadata.create_all)
        async with database.async_session_factory() as db:
            work = Movie(title_cn="Original")
            db.add(work)
            await db.commit()
            work_id = work.id

        async def poster(_url):
            async with database.async_session_factory() as editor:
                work = await editor.get(Movie, work_id)
                work.title_cn = "Manually corrected"
                work.manually_edited_fields = ["title_cn"]
                await editor.commit()
            return "/posters/synthetic.jpg"

        candidate = MetadataCandidate(
            origin="external", content_type="movie", title_cn="Automatic replacement",
            primary_source="tmdb", identity_source="tmdb", external_id="tmdb:1",
            match_path="primary", selectable=True, poster_url="https://example.invalid/poster.jpg",
            metadata={},
        )
        async with database.async_session_factory() as db:
            with patch("app.services.metadata_search.download_and_cache_poster", poster):
                await apply_work_metadata(db, work_id, "movie", candidate, False)
        async with database.async_session_factory() as reader:
            [work] = (await reader.scalars(select(Movie))).all()
            result = {"title": work.title_cn, "manual_fields": work.manually_edited_fields,
                      "expected_title": "Manually corrected", "data": "synthetic candidate; real PostgreSQL sessions"}
            Path(os.environ["PROBE_RESULT_PATH"]).write_text(json.dumps(result, indent=2) + "\n")
            assert work.title_cn == "Manually corrected", result
    finally:
        await database.engine.dispose()


asyncio.run(main())
