"""Additional unit tests for title_cleaner: LLM cache hit/miss, generate_title_regex."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.models.metadata_cache import MetadataCache
from app.services.title_cleaner import (
    clean_title_llm,
    generate_title_regex,
)


@pytest.mark.asyncio
async def test_clean_title_llm_cache_hit(db_session):
    """When a cached entry exists, LLM is NOT called and cached value returned."""
    title = "[Group] 标题 S01E01 [1080p]"
    entry = MetadataCache(
        title=title, source="llm_title", content_type="title_cleaning",
        metadata_json={"clean_title": "标题"},
    )
    db_session.add(entry)
    await db_session.commit()

    with patch("app.services.title_cleaner.call_llm", new_callable=AsyncMock) as m:
        result = await clean_title_llm(title, db_session)
        m.assert_not_called()
    assert result == "标题"


@pytest.mark.asyncio
async def test_clean_title_llm_cache_miss_calls_llm_and_populates_cache(db_session):
    """Cache miss: call LLM, strip quotes, write cache."""
    title = "[X] Show - 01 [1080p]"
    with patch(
        "app.services.title_cleaner.call_llm",
        new_callable=AsyncMock, return_value='"Show Name"',
    ) as m:
        result = await clean_title_llm(title, db_session)
        m.assert_awaited_once()
    assert result == "Show Name"

    # Cache entry was written
    from sqlalchemy import select
    q = await db_session.execute(
        select(MetadataCache).where(MetadataCache.title == title, MetadataCache.source == "llm_title")
    )
    cached = q.scalar_one()
    assert cached.metadata_json["clean_title"] == "Show Name"


@pytest.mark.asyncio
async def test_clean_title_llm_failure_returns_original(db_session):
    title = "title"
    with patch(
        "app.services.title_cleaner.call_llm",
        new_callable=AsyncMock, side_effect=RuntimeError("LLM down"),
    ):
        result = await clean_title_llm(title, db_session)
    assert result == title


@pytest.mark.asyncio
async def test_clean_title_llm_multiline_response_takes_first_line(db_session):
    title = "[A] Big Show - 01"
    with patch(
        "app.services.title_cleaner.call_llm",
        new_callable=AsyncMock,
        return_value="Big Show\n\nSome extra commentary",
    ):
        result = await clean_title_llm(title, db_session)
    assert result == "Big Show"


@pytest.mark.asyncio
async def test_generate_title_regex_empty_entries_returns_none():
    assert await generate_title_regex([]) is None
    assert await generate_title_regex([{"notitle": "x"}]) is None


@pytest.mark.asyncio
async def test_generate_title_regex_strips_code_fences(monkeypatch):
    async def _fake_llm(messages):
        return "```regex\n^\\[([^\\]]+)\\]\\s*(.+?)(?:\\s-|\\[)\n```"
    with patch("app.services.title_cleaner.call_llm", new_callable=AsyncMock, side_effect=_fake_llm):
        pat = await generate_title_regex([{"title": "[A] T - 01 [1080p]"}, {"title": "[B] S - 02"}])
    assert pat is not None
    assert pat.startswith("^")


@pytest.mark.asyncio
async def test_generate_title_regex_invalid_returns_none():
    with patch(
        "app.services.title_cleaner.call_llm",
        new_callable=AsyncMock, return_value="(unclosed[bracket",
    ):
        pat = await generate_title_regex([{"title": "[A] T - 01"}])
    assert pat is None
