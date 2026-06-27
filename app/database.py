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


async def create_tables() -> None:
    """Create all database tables (drop-and-recreate dev strategy)."""
    async with engine.begin() as conn:
        if "sqlite" in settings.database_url:
            # WAL mode allows concurrent reads alongside a single writer and
            # dramatically reduces "database is locked" errors under load.
            await conn.execute(text("PRAGMA journal_mode=WAL"))
        await conn.run_sync(Base.metadata.create_all)
