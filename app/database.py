"""Async SQLAlchemy database setup."""

from collections.abc import AsyncGenerator

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

def _engine_connect_args() -> dict:
    """Extra connection kwargs for SQLite. Ignored for other drivers."""
    if "sqlite" in settings.database_url:
        return {"timeout": 15}  # wait up to 15 s before raising "database is locked"
    return {}


def enable_sqlite_fk(async_engine) -> None:
    """Enable foreign key enforcement for a SQLite async engine."""
    if "sqlite" not in str(async_engine.url):
        return

    @event.listens_for(async_engine.sync_engine, "connect")
    def _set_fk_pragma(dbapi_conn, _record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.close()


engine = create_async_engine(
    settings.database_url,
    echo=settings.debug,
    connect_args=_engine_connect_args(),
)
enable_sqlite_fk(engine)

async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""
    pass


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency for database sessions."""
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def _pre_migrate(conn) -> None:
    """Fix schema issues that require SQLite table rebuilds.

    sqlite_autoindex_* constraints (created by column-level UNIQUE) can only be
    removed by rebuilding the table.  This function detects and handles such cases
    before create_all runs.
    """
    # Only act if file_resources already exists
    tbl = await conn.execute(text(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='file_resources'"
    ))
    if not tbl.fetchone():
        return

    # PRAGMA index_list columns: seq, name, unique, origin, partial
    idx_list = await conn.execute(text("PRAGMA index_list(file_resources)"))
    rows = idx_list.fetchall()
    needs_rebuild = False
    for row in rows:
        idx_name, is_unique, origin = row[1], row[2], row[3]
        # 'u' = UNIQUE constraint (creates sqlite_autoindex), 'pk' = primary key
        if is_unique and origin == "u":
            info = await conn.execute(text(f"PRAGMA index_info(\"{idx_name}\")"))
            cols = [r[2] for r in info.fetchall()]
            if "guid" in cols and "channel_id" not in cols:
                needs_rebuild = True
                break

    if needs_rebuild:
        # Drop so create_all can recreate with the correct compound unique constraint.
        # file_resources only holds derived RSS data — no user-originated data is lost.
        await conn.execute(text("PRAGMA foreign_keys = OFF"))
        await conn.execute(text("DROP TABLE file_resources"))
        await conn.execute(text("PRAGMA foreign_keys = ON"))


async def _migrate_schema(conn) -> None:
    """Add missing columns to existing tables (poor-man's migration)."""
    migrations = [
        # channels
        ("channels", "title_extraction_method",
         "ALTER TABLE channels ADD COLUMN title_extraction_method VARCHAR(20) NOT NULL DEFAULT 'llm'"),
        ("channels", "title_extraction_regex",
         "ALTER TABLE channels ADD COLUMN title_extraction_regex VARCHAR(500)"),
        # file_resources
        ("file_resources", "search_title",
         "ALTER TABLE file_resources ADD COLUMN search_title VARCHAR(512)"),
        ("file_resources", "episode",
         "ALTER TABLE file_resources ADD COLUMN episode INTEGER"),
        ("file_resources", "series_id",
         "ALTER TABLE file_resources ADD COLUMN series_id VARCHAR(36) REFERENCES tv_series(id) ON DELETE SET NULL"),
        # movies — rich metadata fields
        ("movies", "original_title",
         "ALTER TABLE movies ADD COLUMN original_title VARCHAR(512)"),
        ("movies", "poster_url",
         "ALTER TABLE movies ADD COLUMN poster_url VARCHAR(512)"),
        ("movies", "rating",
         "ALTER TABLE movies ADD COLUMN rating REAL"),
        ("movies", "genre",
         "ALTER TABLE movies ADD COLUMN genre TEXT"),
        ("movies", "status",
         "ALTER TABLE movies ADD COLUMN status VARCHAR(100)"),
        # tv_series — rich metadata fields
        ("tv_series", "original_title",
         "ALTER TABLE tv_series ADD COLUMN original_title VARCHAR(512)"),
        ("tv_series", "poster_url",
         "ALTER TABLE tv_series ADD COLUMN poster_url VARCHAR(512)"),
        ("tv_series", "rating",
         "ALTER TABLE tv_series ADD COLUMN rating REAL"),
        ("tv_series", "status",
         "ALTER TABLE tv_series ADD COLUMN status VARCHAR(100)"),
        ("tv_series", "number_of_episodes",
         "ALTER TABLE tv_series ADD COLUMN number_of_episodes INTEGER"),
        ("tv_series", "number_of_seasons",
         "ALTER TABLE tv_series ADD COLUMN number_of_seasons INTEGER"),
        # download_tasks — upload speed added in v0.2
        ("download_tasks", "upload_speed",
         "ALTER TABLE download_tasks ADD COLUMN upload_speed INTEGER NOT NULL DEFAULT 0"),
        # agents — mode added for watchlist support (v0.3)
        ("agents", "mode",
         "ALTER TABLE agents ADD COLUMN mode VARCHAR(20) NOT NULL DEFAULT 'global'"),
    ]

    for table, column, ddl in migrations:
        tbl_check = await conn.execute(text(
            f"SELECT name FROM sqlite_master WHERE type='table' AND name='{table}'"
        ))
        if not tbl_check.fetchone():
            continue
        rows = await conn.execute(text(f"PRAGMA table_info({table})"))
        existing = {row[1] for row in rows}
        if column not in existing:
            await conn.execute(text(ddl))



async def create_tables() -> None:
    """Create all database tables."""
    async with engine.begin() as conn:
        if "sqlite" in settings.database_url:
            # WAL mode allows concurrent reads alongside a single writer and
            # dramatically reduces "database is locked" errors under load.
            await conn.execute(text("PRAGMA journal_mode=WAL"))
        await _pre_migrate(conn)   # rebuild tables whose constraints changed
        await conn.run_sync(Base.metadata.create_all)
        await _migrate_schema(conn)
