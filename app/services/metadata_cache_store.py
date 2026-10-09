"""Exact-title cache lookup and atomic, collision-safe cache writes."""
import logging
import uuid

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.models import metadata_cache as cache
from app.utils.sql_time import UTCNow

logger = logging.getLogger(__name__)


def cache_identity(title: str, source: str):
    return (
        cache.MetadataCache.title_hash == cache.cache_title_hash(title),
        cache.MetadataCache.source == source,
        cache.MetadataCache.title == title,
    )


async def store_cache(db, *, title: str, source: str, content_type: str | None, metadata_json: dict) -> bool:
    dialect = db.get_bind().dialect.name
    if dialect not in {"postgresql", "sqlite"}:
        raise ValueError(f"Unsupported cache backend: {dialect}")
    insert = pg_insert if dialect == "postgresql" else sqlite_insert
    statement = insert(cache.MetadataCache).values(
        id=str(uuid.uuid4()), title=title, title_hash=cache.cache_title_hash(title), source=source,
        content_type=content_type, metadata_json=metadata_json, generation=cache.METADATA_CACHE_GENERATION,
    )
    statement = statement.on_conflict_do_update(
        index_elements=["title_hash", "source"],
        set_={"content_type": statement.excluded.content_type,
              "metadata_json": statement.excluded.metadata_json,
              "generation": statement.excluded.generation, "updated_at": UTCNow()},
        where=cache.MetadataCache.title == statement.excluded.title,
    ).returning(cache.MetadataCache).execution_options(populate_existing=True)
    row = await db.scalar(statement)
    if row is None:
        # A cache miss is safe; serving or replacing another title is not.
        logger.warning("Metadata cache digest collision in source %s", source)
        return False
    return True
