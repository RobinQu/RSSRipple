"""Advance organize configuration in the same transaction as its writers.

ORM changes and bulk ORM UPDATE/DELETE are covered. Migration/admin raw SQL
must explicitly advance the revision if it changes live configuration.
"""
from sqlalchemy import event, inspect, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.models.downloader import DownloaderInstance
from app.models.library import Library
from app.models.organize_configuration import CONFIGURATION_ID, OrganizeConfiguration
from app.models.organize_rule import OrganizeRule
from app.models.storage_volume import StorageVolume

_FIELDS = {
    OrganizeRule: {"priority", "enabled", "filter", "library_id", "path_template", "file_op", "auto_execute"},
    Library: {"volume_id", "root_subpath", "recycle_subpath", "subtitle_lang_map", "media_server_id", "section_key"},
    StorageVolume: {"mount_path"},
    DownloaderInstance: {"download_dir", "volume_id", "volume_subpath"},
}
_TABLES = {model.__tablename__ for model in _FIELDS}


def ensure_configuration(connection):
    insert = pg_insert if connection.dialect.name == "postgresql" else sqlite_insert
    connection.execute(insert(OrganizeConfiguration.__table__).values(
        id=CONFIGURATION_ID, revision=0,
    ).on_conflict_do_nothing(index_elements=["id"]))


def _advance(connection):
    ensure_configuration(connection)
    connection.execute(update(OrganizeConfiguration.__table__).where(
        OrganizeConfiguration.id == CONFIGURATION_ID,
    ).values(revision=OrganizeConfiguration.revision + 1))


@event.listens_for(Session, "before_flush")
def _configuration_flush(session, flush_context, instances):
    for obj in session.new | session.dirty | session.deleted:
        fields = _FIELDS.get(type(obj))
        if fields is None:
            continue
        if obj in session.new or obj in session.deleted or any(
            inspect(obj).attrs[field].history.has_changes() for field in fields
        ):
            _advance(session.connection())
            return


@event.listens_for(Session, "do_orm_execute")
def _configuration_bulk(execute_state):
    if not (execute_state.is_update or execute_state.is_delete):
        return
    table = getattr(execute_state.statement, "table", None)
    if table is not None and table.name in _TABLES:
        _advance(execute_state.session.connection())
