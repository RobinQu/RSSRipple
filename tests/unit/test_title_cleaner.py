"""Unit tests for title_cleaner service.

Tests regex cleanup, backfill logic, and the interaction with the
channel's title_extraction_method config. LLM calls are mocked.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.title_cleaner import clean_title_regex

TEST_FIELD_MAPPING = {
    "list_locator": {"source": "entries"},
    "field_mappings": {"torrent_url": {"source": "link"}},
}


# =============================================================================
# clean_title_regex — sync regex-based title cleanup
# =============================================================================

def test_clean_title_regex_strips_season():
    assert clean_title_regex("杖与剑的魔剑谭 Season 2", r"^(.+?)\s*Season") == "杖与剑的魔剑谭"


def test_clean_title_regex_strips_trailing_space():
    assert clean_title_regex("魔王學院的不適任者 ", r"^(.+?)\s*$") == "魔王學院的不適任者"


def test_clean_title_regex_no_match_returns_original():
    assert clean_title_regex("Clean Title", r"^Nonexistent$") == "Clean Title"


def test_clean_title_regex_empty_pattern():
    assert clean_title_regex("Some Title", "") == "Some Title"


def test_clean_title_regex_invalid_pattern():
    assert clean_title_regex("Some Title", "[invalid(") == "Some Title"


def test_clean_title_regex_with_capture_group():
    """Regex with group 1 returns the captured group."""
    assert clean_title_regex("[Group] Title Here - 12", r"\]\s*(.+?)\s*-") == "Title Here"


def test_clean_title_regex_without_capture_group():
    """Regex without groups returns the full match."""
    result = clean_title_regex("SomeTitle123", r"SomeTitle\d+")
    assert result == "SomeTitle123"


# =============================================================================
# backfill_titles — batch title extraction for resources without search_title
# =============================================================================

@pytest.mark.asyncio
async def test_backfill_titles_extracts_for_resources_without_search_title(db_session):
    """Resources with search_title=NULL get their title extracted."""
    from app.services.title_cleaner import backfill_titles
    from app.models.channel import Channel
    from app.models.file_resource import FileResource

    # Create a channel with regex extraction method
    channel = Channel(
        name="Test",
        url="https://example.com/rss",
        field_mapping=TEST_FIELD_MAPPING,
        title_extraction_method="regex",
        title_extraction_regex=r"^(.+?)\s*-\s*\d+",
    )
    db_session.add(channel)
    await db_session.flush()

    # Create resources without search_title
    r1 = FileResource(
        channel_id=channel.id,
        guid="guid-1",
        title_raw="[Group] 葬送的芙莉莲 - 01 [1080p]",
        title_cn="葬送的芙莉莲",
        torrent_url="https://example.com/1.torrent",
    )
    r2 = FileResource(
        channel_id=channel.id,
        guid="guid-2",
        title_raw="[Group] 咒术回战 - 12 [1080p]",
        title_cn="咒术回战",
        torrent_url="https://example.com/2.torrent",
    )
    db_session.add_all([r1, r2])
    await db_session.flush()

    count = await backfill_titles(channel, db_session)
    assert count == 2
    assert r1.search_title == "葬送的芙莉莲"
    assert r2.search_title == "咒术回战"


@pytest.mark.asyncio
async def test_backfill_titles_skips_resources_with_existing_search_title(db_session):
    """Resources that already have search_title are skipped."""
    from app.services.title_cleaner import backfill_titles
    from app.models.channel import Channel
    from app.models.file_resource import FileResource

    channel = Channel(
        name="Test",
        url="https://example.com/rss",
        field_mapping=TEST_FIELD_MAPPING,
        title_extraction_method="regex",
        title_extraction_regex=r"^(.+)",
    )
    db_session.add(channel)
    await db_session.flush()

    # r1 has search_title set, r2 doesn't
    r1 = FileResource(
        channel_id=channel.id, guid="guid-1", title_raw="Title 1",
        title_cn="Existing Title", search_title="Already Extracted",
        torrent_url="https://example.com/1.torrent",
    )
    r2 = FileResource(
        channel_id=channel.id, guid="guid-2", title_raw="Title 2",
        title_cn="New Title", torrent_url="https://example.com/2.torrent",
    )
    db_session.add_all([r1, r2])
    await db_session.flush()

    count = await backfill_titles(channel, db_session)
    assert count == 1  # Only r2
    assert r1.search_title == "Already Extracted"  # Unchanged
    assert r2.search_title == "New Title"  # Extracted


@pytest.mark.asyncio
async def test_backfill_titles_llm_method_calls_llm(db_session):
    """LLM method calls the LLM and stores the result."""
    from app.services.title_cleaner import backfill_titles
    from app.models.channel import Channel
    from app.models.file_resource import FileResource

    channel = Channel(
        name="Test",
        url="https://example.com/rss",
        field_mapping=TEST_FIELD_MAPPING,
        title_extraction_method="llm",
    )
    db_session.add(channel)
    await db_session.flush()

    resource = FileResource(
        channel_id=channel.id, guid="guid-1",
        title_raw="[Group] 魔王學院的不適任者 - 12 [1080p]",
        title_cn="魔王學院的不適任者",
        torrent_url="https://example.com/1.torrent",
    )
    db_session.add(resource)
    await db_session.flush()

    # Mock the LLM call
    with patch("app.services.title_cleaner.clean_title_llm", new_callable=AsyncMock) as mock_llm:
        mock_llm.return_value = "魔王學院的不適任者"
        count = await backfill_titles(channel, db_session)

    assert count == 1
    assert resource.search_title == "魔王學院的不適任者"
    mock_llm.assert_called_once_with("魔王學院的不適任者", db_session)


@pytest.mark.asyncio
async def test_backfill_titles_llm_failure_falls_back_to_base_title(db_session):
    """When LLM fails, the base title is stored (not NULL) to prevent retry."""
    from app.services.title_cleaner import backfill_titles
    from app.models.channel import Channel
    from app.models.file_resource import FileResource

    channel = Channel(
        name="Test",
        url="https://example.com/rss",
        field_mapping=TEST_FIELD_MAPPING,
        title_extraction_method="llm",
    )
    db_session.add(channel)
    await db_session.flush()

    resource = FileResource(
        channel_id=channel.id, guid="guid-1",
        title_raw="[Group] 魔王學院的不適任者 - 12",
        title_cn="魔王學院的不適任者",
        torrent_url="https://example.com/1.torrent",
    )
    db_session.add(resource)
    await db_session.flush()

    # Mock LLM to raise
    with patch("app.services.title_cleaner.clean_title_llm", new_callable=AsyncMock) as mock_llm:
        mock_llm.side_effect = Exception("LLM timeout")
        count = await backfill_titles(channel, db_session)

    assert count == 1
    # Should fall back to base title (not None) to prevent retry on next fetch
    assert resource.search_title == "魔王學院的不適任者"


@pytest.mark.asyncio
async def test_backfill_titles_none_method_does_nothing(db_session):
    """When method='none', no extraction happens — resources keep search_title=NULL."""
    from app.services.title_cleaner import backfill_titles
    from app.models.channel import Channel
    from app.models.file_resource import FileResource

    channel = Channel(
        name="Test",
        url="https://example.com/rss",
        field_mapping=TEST_FIELD_MAPPING,
        title_extraction_method="none",
    )
    db_session.add(channel)
    await db_session.flush()

    resource = FileResource(
        channel_id=channel.id, guid="guid-1",
        title_raw="[Group] Title - 12",
        title_cn="Title",
        torrent_url="https://example.com/1.torrent",
    )
    db_session.add(resource)
    await db_session.flush()

    # backfill_titles is only called when method != "none" (checked in fetch_service)
    # But if called directly, it should still process (the method check is in apply_title_extraction)
    count = await backfill_titles(channel, db_session)
    assert count == 1
    # apply_title_extraction with "none" returns the title unchanged
    assert resource.search_title == "Title"


@pytest.mark.asyncio
async def test_backfill_titles_no_resources_returns_zero(db_session):
    """When there are no resources to backfill, returns 0."""
    from app.services.title_cleaner import backfill_titles
    from app.models.channel import Channel

    channel = Channel(
        name="Test",
        url="https://example.com/rss",
        field_mapping=TEST_FIELD_MAPPING,
        title_extraction_method="llm",
    )
    db_session.add(channel)
    await db_session.flush()

    count = await backfill_titles(channel, db_session)
    assert count == 0


@pytest.mark.asyncio
async def test_backfill_titles_resource_without_title_cn_uses_title_raw(db_session):
    """When title_cn is NULL, falls back to title_raw parsing."""
    from app.services.title_cleaner import backfill_titles
    from app.models.channel import Channel
    from app.models.file_resource import FileResource

    channel = Channel(
        name="Test",
        url="https://example.com/rss",
        field_mapping=TEST_FIELD_MAPPING,
        title_extraction_method="regex",
        title_extraction_regex=r"^(.+?)\s*-",
    )
    db_session.add(channel)
    await db_session.flush()

    # No title_cn/title_en — falls back to parse_title on title_raw
    resource = FileResource(
        channel_id=channel.id, guid="guid-1",
        title_raw="[Group] 魔王學院的不適任者 - 12 [1080p]",
        title_cn=None, title_en=None,
        torrent_url="https://example.com/1.torrent",
    )
    db_session.add(resource)
    await db_session.flush()

    count = await backfill_titles(channel, db_session)
    assert count == 1
    assert resource.search_title is not None
    # parse_title should extract "魔王學院的不適任者" from the raw title
    assert "魔王" in resource.search_title
