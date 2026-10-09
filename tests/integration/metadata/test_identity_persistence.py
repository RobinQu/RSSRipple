"""Real process/upsert/cache/identity-bag boundary; source and LLM are synthetic."""

import json
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from sqlalchemy import select

from app.models.channel import Channel
from app.models.file_resource import FileResource
from app.models.metadata_cache import MetadataCache
from app.models.movie import Movie
from app.models.work_external_id import WorkExternalId
from app.services import metadata_wiki_judge as wiki
from app.services.metadata_agent import UnifiedMetadataAgent
from tests.unit.test_agent_service import TEST_FIELD_MAPPING
from tests.unit.test_metadata_wiki_judge import _model


@pytest.mark.parametrize("source", ["wikipedia", "wikipedia_react", "tmdb"])
@pytest.mark.parametrize("variant", ["valid", "invalid_primary", "forged_alias", "trusted_alias", "old_cache"])
async def test_process_persists_only_evidenced_identity(db_session, monkeypatch, variant, source):
    primary_source = "tmdb" if source == "tmdb" else "wikipedia"
    channel = Channel(
        id=str(uuid.uuid4()),
        name="identity persistence",
        url=f"https://example.invalid/rss/{str(uuid.uuid4())}",
        field_mapping=TEST_FIELD_MAPPING,
        metadata_source=primary_source,
        metadata_fallback_sources=[],
    )
    resource = FileResource(
        id=str(uuid.uuid4()),
        channel_id=channel.id,
        guid=str(uuid.uuid4()),
        title_raw="Synthetic grounding film release",
        torrent_url="https://example.invalid/file.torrent",
    )
    db_session.add_all([channel, resource])
    await db_session.commit()
    prefix = "tmdb" if source == "tmdb" else "wikipedia:en"
    identity = f"{prefix}:999" if variant == "invalid_primary" else f"{prefix}:123"
    entity = {
        "external_id": identity,
        "external_source": primary_source,
        "title_en": "Synthetic Grounding Film",
        "genre": ["Animation"],
    }
    if variant in ("forged_alias", "trusted_alias"):
        entity["alt_external_ids"] = [{"source": "wikipedia", "id": "wikipedia:ja:999"}]
    final = {
        "found": True,
        "content_type": "movie",
        "clean_title": "Synthetic Grounding Film",
        "matched_entity": entity,
    }
    model = _model(final)
    monkeypatch.setattr(wiki, "_candidate_queries", lambda *args: [("synthetic query", "en")])
    monkeypatch.setattr(
        wiki,
        "_execute_search_wikipedia",
        AsyncMock(return_value={"success": True, "data": [{"page_id": 123, "title": "Unrelated page title"}]}),
    )
    monkeypatch.setattr(
        wiki,
        "_execute_get_wikipedia_page",
        AsyncMock(
            return_value={
                "success": True,
                "data": {
                    "page_id": 123,
                    "categories": ["Animated films"],
                    "summary": "Synthetic evidence for an animated film.",
                    "langlink_pageids": {"ja": 456} if variant == "trusted_alias" else {},
                },
            }
        ),
    )
    agent = UnifiedMetadataAgent()
    agent._model = model
    if source in ("tmdb", "wikipedia_react"):
        monkeypatch.setattr("app.services.runtime_config._overrides", {"tmdb_api_key": ""})
        if source == "tmdb":
            observed = ToolMessage(
                content=json.dumps({"success": True, "data": [{"external_id": "tmdb:123", "content_type": "movie"}]}),
                name="search_tmdb",
                tool_call_id="search",
            )
        else:
            # Exercise production judge -> ReAct fallback, not a mocked validator.
            monkeypatch.setattr(wiki, "_candidate_queries", lambda *args: [])
            observed = ToolMessage(
                content=json.dumps(
                    {
                        "success": True,
                        "data": {
                            "page_id": 123,
                            "url": "https://en.wikipedia.org/wiki/Synthetic_film",
                            "categories": ["Animated films"],
                            "langlink_pageids": {"ja": 456} if variant == "trusted_alias" else {},
                        },
                    }
                ),
                name="get_wikipedia_page",
                tool_call_id="page",
            )
        messages = [
            observed,
            AIMessage(
                content="", tool_calls=[{"name": "finalize", "id": "final", "args": {"result_json": json.dumps(final)}}]
            ),
        ]
        model = MagicMock(ainvoke=AsyncMock(return_value={"messages": messages}))
        agent._agent_for_source = MagicMock(return_value=model)
    if variant == "old_cache":
        db_session.add(
            MetadataCache(
                title=resource.title_raw,
                source=f"metadata_agent:{primary_source}",
                generation=6,
                content_type="movie",
                metadata_json={**final, "matched_entity": {**entity, "external_id": f"{prefix}:999"}},
            )
        )
        await db_session.commit()
    result = await agent.process(resource, channel, db_session)
    await db_session.commit()
    ids = (await db_session.scalars(select(WorkExternalId))).all()
    assert all(row.external_id not in {"wikipedia:en:999", "wikipedia:ja:999", "tmdb:999"} for row in ids)
    if variant == "invalid_primary":
        assert not result.found and resource.movie_id is None
        assert await db_session.scalar(select(Movie.id)) is None
        assert ids == []
    else:
        assert result.found and resource.movie_id is not None
        expected_ids = {f"{prefix}:123"}
        if variant == "trusted_alias" and primary_source == "wikipedia":
            expected_ids.add("wikipedia:ja:456")
        assert {row.external_id for row in ids} == expected_ids
        assert len((await db_session.scalars(select(Movie))).all()) == 1
        original_id = resource.movie_id
        await agent.process(resource, channel, db_session)
        await db_session.commit()
        assert resource.movie_id == original_id
        assert len((await db_session.scalars(select(Movie))).all()) == 1
        assert {row.external_id for row in (await db_session.scalars(select(WorkExternalId))).all()} == expected_ids
    model.ainvoke.assert_awaited_once()
