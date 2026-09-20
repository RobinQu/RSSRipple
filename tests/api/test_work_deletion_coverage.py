"""A deleted work must not silently shrink a known batch into a smaller batch."""

import uuid

from sqlalchemy import select

from app.models.file_resource import FileResource
from app.models.movie import Movie
from app.models.resource_file_assignment import ResourceFileAssignment
from app.models.resource_work_link import ResourceWorkLink
from app.services.resource_coverage import batch_coverage, load_batch_coverage


async def test_delete_preserves_unassigned_file_and_unknown_batch(
    client, db_session_factory, sample_channel, sample_movie
):
    async with db_session_factory() as db:
        survivor = Movie(title_cn="Synthetic surviving movie")
        resource = FileResource(
            channel_id=sample_channel.id, guid=str(uuid.uuid4()),
            title_raw="Synthetic two movie batch", torrent_url="magnet:?xt=urn:btih:synthetic",
            is_batch=True, batch_scope="movies",
        )
        db.add_all([survivor, resource])
        await db.flush()
        db.add_all([
            ResourceWorkLink(resource_id=resource.id, movie_id=work_id, source="auto")
            for work_id in (sample_movie.id, survivor.id)
        ])
        db.add_all([
            ResourceFileAssignment(resource_id=resource.id, movie_id=work_id, file_path=path, source="auto")
            for work_id, path in [(sample_movie.id, "deleted.mkv"), (survivor.id, "surviving.mkv")]
        ])
        await db.commit()
        rid = resource.id
        await load_batch_coverage(db, [resource])
        assert batch_coverage(resource) is not None
    response = await client.delete(f"/api/v1/movies/{sample_movie.id}")
    assert response.status_code == 200, response.text
    async with db_session_factory() as db:
        resource = await db.get(FileResource, rid)
        await load_batch_coverage(db, [resource])
        rows = list(await db.scalars(select(ResourceFileAssignment).where(ResourceFileAssignment.resource_id == rid)))
        assert {row.file_path for row in rows} == {"deleted.mkv", "surviving.mkv"}
        assert next(row for row in rows if row.file_path == "deleted.mkv").movie_id is None
        assert batch_coverage(resource) is None
