"""Real DB transaction tests for organize configuration invalidation."""
from sqlalchemy import select, update

from app.models.downloader import DownloaderInstance
from app.models.library import Library
from app.models.organize_configuration import CONFIGURATION_ID, OrganizeConfiguration
from app.models.storage_volume import StorageVolume


async def revision(db):
    return await db.scalar(select(OrganizeConfiguration.revision).where(
        OrganizeConfiguration.id == CONFIGURATION_ID,
    ))


async def test_config_revision_is_seeded_and_transactional(db_session):
    from app.services.organize_config_events import ensure_configuration

    await (await db_session.connection()).run_sync(ensure_configuration)
    await db_session.commit()
    assert await revision(db_session) == 0
    volume = StorageVolume(name="media", mount_path="/test/media")
    db_session.add(volume)
    await db_session.commit()
    initial = await revision(db_session)
    assert initial == 1
    volume.remark = "display-only change"
    await db_session.commit()
    assert await revision(db_session) == initial
    volume.mount_path = "/test/new-media"
    await db_session.flush()
    assert await revision(db_session) == initial + 1
    await db_session.rollback()
    assert await revision(db_session) == initial


async def test_bulk_library_update_invalidates_in_same_transaction(db_session):
    library = Library(name="media", root_subpath="tv")
    db_session.add(library)
    await db_session.commit()
    initial = await revision(db_session)
    await db_session.execute(update(Library).where(Library.id == library.id).values(root_subpath="changed"))
    assert await revision(db_session) == initial + 1
    await db_session.rollback()
    assert await revision(db_session) == initial


async def test_downloader_status_does_not_invalidate_plans(db_session):
    downloader = DownloaderInstance(
        name="test", type="transmission", url="http://example.invalid", download_dir="/downloads",
    )
    db_session.add(downloader)
    await db_session.commit()
    initial = await revision(db_session)
    downloader.status = "connected"
    await db_session.commit()
    assert await revision(db_session) == initial
    downloader.download_dir = "/downloads/new"
    await db_session.commit()
    assert await revision(db_session) == initial + 1
