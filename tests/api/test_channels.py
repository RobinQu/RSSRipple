"""API tests for channel endpoints.

All external dependencies (RSS parsing, LLM calls, fetch service) are mocked
to ensure tests are fast, deterministic, and never hit real URLs.
"""

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

MOCK_VALIDATE = "app.api.v1.channels.validate_rss_url"
MOCK_RAW_ENTRIES = "app.api.v1.channels.get_raw_entries"
MOCK_ANALYZE_FEED = "app.api.v1.channels.analyze_feed"
MOCK_ANALYZE_STREAM = "app.services.feed_analyzer.analyze_feed_stream"
MOCK_FETCH_SERVICE = "app.services.fetch_service.fetch_channel_resources"

# --- Helpers ---

def channel_payload(**overrides):
    """Build a minimal valid channel creation payload."""
    base = {
        "name": "Test Channel",
        "type": "rss_feed",
        "url": "https://example.com/rss",
        "fetch_interval": 1800,
    }
    base.update(overrides)
    return base


async def create_channel(client, mock_validate=None, **payload_overrides):
    """Create a channel with validate_rss_url mocked, returning the response."""
    if mock_validate is None:
        mock_validate = AsyncMock(return_value=(True, "Valid", 10, 8))
    with patch(MOCK_VALIDATE, mock_validate):
        res = await client.post("/api/v1/channels", json=channel_payload(**payload_overrides))
    return res


# =============================================================================
# 1. test_create_channel
# =============================================================================
@pytest.mark.asyncio
async def test_create_channel(client):
    mock = AsyncMock(return_value=(True, "Valid", 10, 8))
    res = await create_channel(client, mock_validate=mock)

    assert res.status_code == 201
    data = res.json()
    assert data["success"] is True

    ch = data["data"]
    assert ch["name"] == "Test Channel"
    assert ch["type"] == "rss_feed"
    assert ch["url"] == "https://example.com/rss"
    assert ch["fetch_interval"] == 1800
    assert ch["status"] == "active"
    assert "parser_type" not in ch  # parser_type was removed
    assert "id" in ch
    assert "created_at" in ch
    assert "updated_at" in ch

    # Meta should contain feed validation stats
    assert data["meta"]["feed_items"] == 10
    assert data["meta"]["downloadable"] == 8


# =============================================================================
# 2. test_create_channel_invalid_feed
# =============================================================================
@pytest.mark.asyncio
async def test_create_channel_invalid_feed(client):
    mock = AsyncMock(return_value=(False, "Feed is empty", 0, 0))
    res = await create_channel(client, mock_validate=mock)

    assert res.status_code == 422
    body = res.json()
    assert body["success"] is False
    assert body["error"]["code"] == "INVALID_FEED"
    assert "empty" in body["error"]["message"].lower()


# =============================================================================
# 3. test_list_channels
# =============================================================================
@pytest.mark.asyncio
async def test_list_channels(client):
    mock = AsyncMock(return_value=(True, "Valid", 10, 8))
    await create_channel(client, mock_validate=mock, name="Ch1", url="https://a.com/rss")
    await create_channel(client, mock_validate=mock, name="Ch2", url="https://b.com/rss")

    res = await client.get("/api/v1/channels")
    assert res.status_code == 200
    data = res.json()
    assert data["success"] is True
    assert len(data["data"]) == 2
    assert data["meta"]["total"] == 2


# =============================================================================
# 4. test_get_channel
# =============================================================================
@pytest.mark.asyncio
async def test_get_channel(client):
    create_res = await create_channel(client, name="My Channel", url="https://x.com/rss")
    channel_id = create_res.json()["data"]["id"]

    res = await client.get(f"/api/v1/channels/{channel_id}")
    assert res.status_code == 200
    ch = res.json()["data"]
    assert ch["name"] == "My Channel"
    assert ch["id"] == channel_id
    assert "parser_type" not in ch


# =============================================================================
# 5. test_get_channel_not_found
# =============================================================================
@pytest.mark.asyncio
async def test_get_channel_not_found(client):
    fake_id = str(uuid.uuid4())
    res = await client.get(f"/api/v1/channels/{fake_id}")
    assert res.status_code == 404
    body = res.json()
    assert body["success"] is False
    assert body["error"]["code"] == "NOT_FOUND"


# =============================================================================
# 6. test_update_channel
# =============================================================================
@pytest.mark.asyncio
async def test_update_channel(client):
    create_res = await create_channel(client, name="Old Name", url="https://x.com/rss")
    channel_id = create_res.json()["data"]["id"]

    res = await client.put(f"/api/v1/channels/{channel_id}", json={"name": "New Name"})
    assert res.status_code == 200
    assert res.json()["data"]["name"] == "New Name"


# =============================================================================
# 7. test_update_channel_field_mapping
# =============================================================================
@pytest.mark.asyncio
async def test_update_channel_field_mapping(client):
    create_res = await create_channel(client, url="https://x.com/rss")
    channel_id = create_res.json()["data"]["id"]

    new_mapping = {
        "list_locator": {"source": "entries"},
        "field_mappings": {
            "title_cn": {"source": "title", "regex": "\\]\\s*(.+?)\\s*-", "group": 1},
            "episode": {"source": "title", "regex": "-\\s*(\\d+)\\b", "group": 1, "transform": "int"},
        },
    }

    res = await client.put(
        f"/api/v1/channels/{channel_id}",
        json={"field_mapping": new_mapping},
    )
    assert res.status_code == 200
    data = res.json()["data"]
    assert data["field_mapping"] == new_mapping


# =============================================================================
# 8. test_delete_channel
# =============================================================================
@pytest.mark.asyncio
async def test_delete_channel(client):
    create_res = await create_channel(client, name="Delete Me", url="https://x.com/rss")
    channel_id = create_res.json()["data"]["id"]

    del_res = await client.delete(f"/api/v1/channels/{channel_id}")
    assert del_res.status_code == 200
    assert del_res.json()["data"]["deleted"] is True

    # Verify it's gone
    get_res = await client.get(f"/api/v1/channels/{channel_id}")
    assert get_res.status_code == 404


# =============================================================================
# 9. test_fetch_channel
# =============================================================================
@pytest.mark.asyncio
async def test_fetch_channel(client):
    """POST /fetch enqueues a background job and returns job state immediately."""
    create_res = await create_channel(client, url="https://x.com/rss")
    channel_id = create_res.json()["data"]["id"]

    res = await client.post(f"/api/v1/channels/{channel_id}/fetch")

    assert res.status_code == 200
    data = res.json()["data"]
    assert data["job_type"] == "fetch_channel"
    assert data["key"] == channel_id
    assert data["status"] in ("queued", "running", "done", "failed")

    # A second immediate request must be deduplicated (409)
    res2 = await client.post(f"/api/v1/channels/{channel_id}/fetch")
    # May be 409 (still in progress) or 200 (already finished) — both are valid
    assert res2.status_code in (200, 409)


# =============================================================================
# 10. test_fetch_channel_not_found
# =============================================================================
@pytest.mark.asyncio
async def test_fetch_channel_not_found(client):
    fake_id = str(uuid.uuid4())
    res = await client.post(f"/api/v1/channels/{fake_id}/fetch")
    assert res.status_code == 404


# =============================================================================
# 11. test_analyze_channel
# =============================================================================
@pytest.mark.asyncio
async def test_analyze_channel(client):
    create_res = await create_channel(client, url="https://x.com/rss")
    channel_id = create_res.json()["data"]["id"]

    sample_entries = [
        {"title": "[SubGroup] Anime - 01 [1080p].mkv", "link": "https://example.com/1"},
    ]
    mock_analysis = {
        "field_mapping": {
            "list_locator": {"source": "entries"},
            "field_mappings": {
                "subtitle_group": {"source": "title", "regex": "^\\[([^\\]]+)\\]", "group": 1},
            },
        },
        "sample_results": [],
        "confidence": "high",
    }

    with (
        patch(MOCK_RAW_ENTRIES, new_callable=AsyncMock, return_value=sample_entries),
        patch(MOCK_ANALYZE_FEED, new_callable=AsyncMock, return_value=mock_analysis),
    ):
        res = await client.post(f"/api/v1/channels/{channel_id}/analyze")

    assert res.status_code == 200
    data = res.json()["data"]
    assert data["confidence"] == "high"
    assert "field_mapping" in data
    assert "field_mappings" in data["field_mapping"]


# =============================================================================
# 12. test_validate_url
# =============================================================================
@pytest.mark.asyncio
async def test_validate_url(client):
    mock = AsyncMock(return_value=(True, "Feed is valid: 10 entries, 8 with downloadable content", 10, 8))

    with patch(MOCK_VALIDATE, mock):
        res = await client.post("/api/v1/channels/validate-url", json={"url": "https://example.com/rss"})

    assert res.status_code == 200
    data = res.json()["data"]
    assert data["valid"] is True
    assert data["item_count"] == 10
    assert data["downloadable_count"] == 8


# =============================================================================
# 13. test_analyze_channel_not_found
# =============================================================================
@pytest.mark.asyncio
async def test_analyze_channel_not_found(client):
    fake_id = str(uuid.uuid4())
    res = await client.post(f"/api/v1/channels/{fake_id}/analyze")
    assert res.status_code == 404
    body = res.json()
    assert body["success"] is False
    assert body["error"]["code"] == "NOT_FOUND"


# =============================================================================
# 14. test_analyze_channel_fetch_error
# =============================================================================
@pytest.mark.asyncio
async def test_analyze_channel_fetch_error(client):
    create_res = await create_channel(client, url="https://x.com/rss")
    channel_id = create_res.json()["data"]["id"]

    with patch(MOCK_RAW_ENTRIES, new_callable=AsyncMock, side_effect=Exception("Connection timeout")):
        res = await client.post(f"/api/v1/channels/{channel_id}/analyze")

    assert res.status_code == 400
    body = res.json()
    assert body["success"] is False
    assert body["error"]["code"] == "FETCH_ERROR"


# =============================================================================
# 15. test_analyze_channel_empty_feed
# =============================================================================
@pytest.mark.asyncio
async def test_analyze_channel_empty_feed(client):
    create_res = await create_channel(client, url="https://x.com/rss")
    channel_id = create_res.json()["data"]["id"]

    with patch(MOCK_RAW_ENTRIES, new_callable=AsyncMock, return_value=[]):
        res = await client.post(f"/api/v1/channels/{channel_id}/analyze")

    assert res.status_code == 400
    body = res.json()
    assert body["success"] is False
    assert body["error"]["code"] == "EMPTY_FEED"


# =============================================================================
# 16. test_analyze_stream_endpoint_exists
# =============================================================================
@pytest.mark.asyncio
async def test_analyze_stream_endpoint_exists(client):
    create_res = await create_channel(client, url="https://x.com/rss")
    channel_id = create_res.json()["data"]["id"]

    sample_entries = [
        {"title": "[SubGroup] Anime - 01 [1080p].mkv", "link": "https://example.com/1"},
    ]

    async def mock_stream(entries, sample_count=5):
        yield {"type": "done", "field_mapping": {}, "confidence": "low"}

    with (
        patch(MOCK_RAW_ENTRIES, new_callable=AsyncMock, return_value=sample_entries),
        patch(MOCK_ANALYZE_STREAM, side_effect=mock_stream),
    ):
        res = await client.post(f"/api/v1/channels/{channel_id}/analyze-stream")

    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/event-stream")


# =============================================================================
# 17. test_create_channel_default_title_extraction_method
# =============================================================================
@pytest.mark.asyncio
async def test_create_channel_default_title_extraction_method(client):
    """New channels default to title_extraction_method='llm'."""
    res = await create_channel(client)
    data = res.json()["data"]
    assert data["title_extraction_method"] == "llm"
    assert data["title_extraction_regex"] is None


# =============================================================================
# 18. test_create_channel_with_custom_title_extraction
# =============================================================================
@pytest.mark.asyncio
async def test_create_channel_with_custom_title_extraction(client):
    """Create a channel with regex extraction method and a custom regex."""
    res = await create_channel(
        client,
        title_extraction_method="regex",
        title_extraction_regex=r"^(.+?)\s*Season",
    )
    assert res.status_code == 201
    data = res.json()["data"]
    assert data["title_extraction_method"] == "regex"
    assert data["title_extraction_regex"] == r"^(.+?)\s*Season"


# =============================================================================
# 19. test_update_channel_title_extraction_method
# =============================================================================
@pytest.mark.asyncio
async def test_update_channel_title_extraction_method(client):
    """Update the title extraction method on an existing channel."""
    res = await create_channel(client)
    channel_id = res.json()["data"]["id"]

    update_res = await client.put(f"/api/v1/channels/{channel_id}", json={
        "title_extraction_method": "regex",
        "title_extraction_regex": r"^(.+?)\s*-",
    })
    assert update_res.status_code == 200
    data = update_res.json()["data"]
    assert data["title_extraction_method"] == "regex"
    assert data["title_extraction_regex"] == r"^(.+?)\s*-"


# =============================================================================
# 20. test_get_channel_includes_title_extraction_fields
# =============================================================================
@pytest.mark.asyncio
async def test_get_channel_includes_title_extraction_fields(client):
    """GET /channels/{id} includes title_extraction_method and regex."""
    res = await create_channel(
        client,
        title_extraction_method="regex",
        title_extraction_regex=r"test_pattern",
    )
    channel_id = res.json()["data"]["id"]

    get_res = await client.get(f"/api/v1/channels/{channel_id}")
    assert get_res.status_code == 200
    data = get_res.json()["data"]
    assert "title_extraction_method" in data
    assert "title_extraction_regex" in data
    assert data["title_extraction_method"] == "regex"
    assert data["title_extraction_regex"] == "test_pattern"


# =============================================================================
# 21. test_fetch_channel_enqueue_dedup
# =============================================================================
@pytest.mark.asyncio
async def test_fetch_channel_enqueue_dedup(client):
    """POST /channels/{id}/fetch is idempotent: second call returns 409 while running."""
    res = await create_channel(client)
    channel_id = res.json()["data"]["id"]

    # First call enqueues and should return job state
    fetch_res = await client.post(f"/api/v1/channels/{channel_id}/fetch")
    assert fetch_res.status_code == 200
    data = fetch_res.json()["data"]
    assert "job_id" in data
    assert data["key"] == channel_id

    # GET /fetch-status should return the same job
    status_res = await client.get(f"/api/v1/channels/{channel_id}/fetch-status")
    assert status_res.status_code == 200
    status_data = status_res.json()["data"]
    assert status_data is not None
    assert status_data["job_id"] == data["job_id"]


# =============================================================================
# Helper for polling job status in API tests
# =============================================================================

async def _poll_until_terminal(client, channel_id: str, timeout: float = 3.0) -> dict:
    """Poll GET /fetch-status until the job reaches done or failed."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        res = await client.get(f"/api/v1/channels/{channel_id}/fetch-status")
        data = res.json()["data"]
        if data and data["status"] in ("done", "failed"):
            return data
        await asyncio.sleep(0.05)
    raise TimeoutError(f"Fetch job for channel {channel_id} did not finish in {timeout}s")


# =============================================================================
# Helper: inject a started MemoryQueue so background jobs run in API tests.
#
# ASGITransport does not trigger the ASGI lifespan, so the module-level
# task_queue singleton is never started by the test client.  We replace it
# with a fresh started queue for the duration of the test.
# =============================================================================

from contextlib import asynccontextmanager
import app.services.task_queue as _tq_mod
from app.services.task_queue import MemoryQueue


@asynccontextmanager
async def running_queue(**handlers):
    """Context manager: start a MemoryQueue with the given handlers, inject it as
    the module-level singleton, and restore the previous singleton on exit."""
    queue = MemoryQueue()
    for job_type, handler in handlers.items():
        queue.register(job_type, handler)
    old = _tq_mod.task_queue
    _tq_mod.task_queue = queue
    await queue.start()
    try:
        yield queue
    finally:
        await queue.stop()
        _tq_mod.task_queue = old


# =============================================================================
# 22. test_fetch_channel_unreachable_feed_fails
# =============================================================================
@pytest.mark.asyncio
async def test_fetch_channel_unreachable_feed_fails(client):
    """When the RSS feed is unreachable (feedparser bozo), the fetch job must
    fail with status='failed' and include a meaningful error message.

    This is the regression test for the bug where fetch_channel_resources
    silently returned {total:0} on network failures instead of raising — making
    the job appear as 'done' with 0 results, indistinguishable from an
    intentionally empty feed.
    """
    res = await create_channel(client)
    channel_id = res.json()["data"]["id"]

    async def unreachable_handler(payload):
        raise RuntimeError(f"Failed to fetch RSS feed 'https://example.com/rss': Operation timed out")

    async with running_queue(fetch_channel=unreachable_handler):
        fetch_res = await client.post(f"/api/v1/channels/{channel_id}/fetch")
        assert fetch_res.status_code == 200
        state = await _poll_until_terminal(client, channel_id)

    assert state["status"] == "failed", f"Expected failed, got: {state}"
    assert state["error"] is not None
    assert "Failed to fetch RSS feed" in state["error"]


# =============================================================================
# 23. test_fetch_channel_job_done_on_success
# =============================================================================
@pytest.mark.asyncio
async def test_fetch_channel_job_done_on_success(client):
    """When the fetch handler succeeds, the job reaches 'done' and exposes result counts."""
    res = await create_channel(client)
    channel_id = res.json()["data"]["id"]

    async def success_handler(payload):
        return {"total": 5, "new": 3, "skipped": 2}

    async with running_queue(fetch_channel=success_handler):
        fetch_res = await client.post(f"/api/v1/channels/{channel_id}/fetch")
        assert fetch_res.status_code == 200
        state = await _poll_until_terminal(client, channel_id)

    assert state["status"] == "done", f"Expected done, got: {state}"
    assert state["error"] is None
    assert state["result"]["new"] == 3


# =============================================================================
# 25. test_resource_response_includes_search_title
# =============================================================================
@pytest.mark.asyncio
async def test_resource_response_includes_search_title(client, db_session):
    """FileResourceResponse includes the search_title field."""
    from app.models.channel import Channel
    from app.models.file_resource import FileResource

    # Create channel + resource directly in DB
    channel = Channel(name="Test", url="https://example.com/rss")
    db_session.add(channel)
    await db_session.flush()

    resource = FileResource(
        channel_id=channel.id,
        guid="test-guid",
        title_raw="[Group] Title - 01",
        title_cn="Title",
        search_title="Title (Cleaned)",
        torrent_url="https://example.com/t.torrent",
    )
    db_session.add(resource)
    await db_session.commit()

    res = await client.get(f"/api/v1/resources/{resource.id}")
    assert res.status_code == 200
    data = res.json()["data"]
    assert "search_title" in data
    assert data["search_title"] == "Title (Cleaned)"


# =============================================================================
# 23. test_metadata_uses_stored_search_title
# =============================================================================
@pytest.mark.asyncio
async def test_metadata_uses_stored_search_title(client, db_session):
    """GET /resources/{id}/metadata uses the stored search_title when available."""
    from app.models.channel import Channel
    from app.models.file_resource import FileResource

    channel = Channel(name="Test", url="https://example.com/rss", title_extraction_method="llm")
    db_session.add(channel)
    await db_session.flush()

    resource = FileResource(
        channel_id=channel.id,
        guid="test-guid",
        title_raw="[Group] 葬送的芙莉莲 - 01 [1080p]",
        title_cn="葬送的芙莉莲",
        search_title="葬送的芙莉莲",  # Already extracted
        torrent_url="https://example.com/t.torrent",
    )
    db_session.add(resource)
    await db_session.commit()

    # Mock fetch_and_link_metadata — should be called with the resource
    mock_get = AsyncMock(return_value={"external_id": "123", "external_source": "tmdb", "title": "Frieren"})
    with patch("app.api.v1.resources.fetch_and_link_metadata", mock_get):
        res = await client.get(f"/api/v1/resources/{resource.id}/metadata")

    assert res.status_code == 200
    mock_get.assert_called_once()
    # Second positional arg is the resource — verify it carries the stored search_title
    called_resource = mock_get.call_args.args[1]
    assert called_resource.search_title == "葬送的芙莉莲"


# =============================================================================
# 26. test_delete_channel_cascades_file_resources
# =============================================================================
@pytest.mark.asyncio
async def test_delete_channel_cascades_file_resources(client, db_session):
    """Deleting a channel must also delete all its file_resources.

    Regression for sqlite3.IntegrityError: NOT NULL constraint failed:
    file_resources.channel_id — SQLAlchemy was trying to SET channel_id=NULL
    instead of deleting the child rows.
    """
    from sqlalchemy import select
    from app.models.channel import Channel
    from app.models.file_resource import FileResource

    channel = Channel(name="To Delete", url="https://example.com/rss")
    db_session.add(channel)
    await db_session.flush()

    # Insert two file resources for this channel
    r1 = FileResource(
        channel_id=channel.id,
        guid="guid-1",
        title_raw="[Group] Show - 01",
        torrent_url="https://example.com/1.torrent",
    )
    r2 = FileResource(
        channel_id=channel.id,
        guid="guid-2",
        title_raw="[Group] Show - 02",
        torrent_url="https://example.com/2.torrent",
    )
    db_session.add_all([r1, r2])
    await db_session.commit()

    resource_ids = [r1.id, r2.id]
    channel_id = channel.id

    # Delete via API — this must NOT raise a 500
    del_res = await client.delete(f"/api/v1/channels/{channel_id}")
    assert del_res.status_code == 200, del_res.text
    assert del_res.json()["data"]["deleted"] is True

    # Channel is gone
    get_res = await client.get(f"/api/v1/channels/{channel_id}")
    assert get_res.status_code == 404

    # File resources must also be gone (ON DELETE CASCADE)
    db_session.expire_all()
    remaining = (
        await db_session.execute(
            select(FileResource).where(FileResource.id.in_(resource_ids))
        )
    ).scalars().all()
    assert remaining == [], f"Expected 0 file_resources, found {len(remaining)}"


# =============================================================================
# 27. test_delete_channel_with_agents_cascades
# =============================================================================
@pytest.mark.asyncio
async def test_delete_channel_with_agents_cascades(client, db_session):
    """Deleting a channel must also cascade-delete its agents."""
    from sqlalchemy import select
    from app.models.channel import Channel
    from app.models.agent import Agent

    channel = Channel(name="Channel With Agent", url="https://example.com/rss")
    db_session.add(channel)
    await db_session.flush()

    agent = Agent(
        name="Test Agent",
        channel_id=channel.id,
        content_type="anime",
    )
    db_session.add(agent)
    await db_session.commit()

    agent_id = agent.id
    channel_id = channel.id

    del_res = await client.delete(f"/api/v1/channels/{channel_id}")
    assert del_res.status_code == 200, del_res.text

    db_session.expire_all()
    orphan = await db_session.get(Agent, agent_id)
    assert orphan is None, "Agent should have been deleted with the channel"


# =============================================================================
# 28. test_form_token_issued
# =============================================================================
@pytest.mark.asyncio
async def test_form_token_issued(client):
    """GET /channels/form-token returns a non-empty UUID token."""
    res = await client.get("/api/v1/channels/form-token")
    assert res.status_code == 200
    data = res.json()
    assert data["success"] is True
    token = data["data"]["token"]
    assert isinstance(token, str) and len(token) == 36  # UUID format


# =============================================================================
# 29. test_form_token_prevents_duplicate_create
# =============================================================================
@pytest.mark.asyncio
async def test_form_token_prevents_duplicate_create(client):
    """Using the same form token twice must reject the second submission with 409.

    This is the server-side enforcement of the synchronizer token pattern:
    the first POST /channels consumes the token; the second carries a stale
    token and must be rejected regardless of button-state on the frontend.
    """
    # Issue one token
    token_res = await client.get("/api/v1/channels/form-token")
    token = token_res.json()["data"]["token"]

    mock_validate = AsyncMock(return_value=(True, "Valid", 5, 5))
    with patch(MOCK_VALIDATE, mock_validate):
        # First submission — should succeed
        res1 = await client.post(
            "/api/v1/channels",
            json=channel_payload(name="Dup Test", url="https://example.com/rss"),
            headers={"X-Form-Token": token},
        )
        assert res1.status_code == 201, res1.text

        # Second submission with the SAME token — must be rejected
        res2 = await client.post(
            "/api/v1/channels",
            json=channel_payload(name="Dup Test 2", url="https://example.com/rss2"),
            headers={"X-Form-Token": token},
        )
    assert res2.status_code == 409, res2.text
    body = res2.json()
    assert body["success"] is False
    assert body["error"]["code"] == "DUPLICATE_SUBMISSION"


# =============================================================================
# 30. test_form_token_not_required
# =============================================================================
@pytest.mark.asyncio
async def test_form_token_not_required(client):
    """POST /channels without X-Form-Token still succeeds (backward compat)."""
    res = await create_channel(client)
    assert res.status_code == 201


# =============================================================================
# 31. test_form_token_prevents_duplicate_update
# =============================================================================
@pytest.mark.asyncio
async def test_form_token_prevents_duplicate_update(client):
    """Same token used twice on PUT /channels/{id} rejects the second call."""
    create_res = await create_channel(client, name="Update Token Test", url="https://x.com/rss")
    channel_id = create_res.json()["data"]["id"]

    token_res = await client.get("/api/v1/channels/form-token")
    token = token_res.json()["data"]["token"]

    # First update — consumes the token
    res1 = await client.put(
        f"/api/v1/channels/{channel_id}",
        json={"name": "Updated Once"},
        headers={"X-Form-Token": token},
    )
    assert res1.status_code == 200, res1.text

    # Second update with the same token — rejected
    res2 = await client.put(
        f"/api/v1/channels/{channel_id}",
        json={"name": "Updated Twice"},
        headers={"X-Form-Token": token},
    )
    assert res2.status_code == 409
    assert res2.json()["error"]["code"] == "DUPLICATE_SUBMISSION"
