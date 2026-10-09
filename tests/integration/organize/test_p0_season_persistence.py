"""P0-3 model boundary -> real ReAct graph -> validator -> repository -> DB.

The chat model and source response are substituted. Raw input comes from a captured
case; the model verdict and season evidence are explicit fault injections,
not recorded source answers or semantic gold for the captured resource.
"""

import json
import socket
import uuid
from unittest.mock import AsyncMock

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from sqlalchemy import select

from app.models.channel import Channel
from app.models.file_resource import FileResource
from app.models.metadata_cache import MetadataCache
from app.models.series import TVSeries
from app.services.metadata_agent import UnifiedMetadataAgent
from tests.metadata_corpus.dataset import load_corpus


class ToolModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    attempts = []

    def reject(*args, **kwargs):
        attempts.append("external connection")
        raise AssertionError("model fault-injection test must remain offline")

    monkeypatch.setattr(socket.socket, "connect", reject)
    monkeypatch.setattr(socket.socket, "connect_ex", reject)
    yield
    assert not attempts, "production code swallowed an unplanned network attempt"


@pytest.mark.parametrize("inferred,count,expected", [
    (1, 2, 1), (99, 2, None), (99, None, None), (99, 1, 1),
    (None, None, None), (None, 1, 1),
])
async def test_model_season_cannot_pollute_persisted_work(db_session, monkeypatch, inferred, count, expected):
    case = next(case for case in load_corpus()[1]["cases"]
                if case["id"] == "f79ef2eb-02d5-42d3-80dc-70dd3c1d733b")
    channel = Channel(id=str(uuid.uuid4()), name="season-boundary", type="rss_feed",
                      url=f"https://unused.invalid/feed/{str(uuid.uuid4())}", metadata_source="tmdb",
                      metadata_fallback_sources=[], default_is_anime=True, field_mapping={
                          "list_locator": {"source": "entries"},
                          "field_mappings": {"torrent_url": {"source": "link"}},
                      })
    resource = FileResource(id=str(uuid.uuid4()), channel_id=channel.id, guid=str(uuid.uuid4()),
                            title_raw=case["input"]["title_raw"], torrent_url="magnet:?xt=urn:btih:test")
    db_session.add_all([channel, resource])
    await db_session.commit()
    entity = {"title_cn": "季号边界测试", "external_source": "tmdb", "external_id": "tmdb:999999991",
              "content_type": "tv", "genre": ["Animation"]}
    if count is not None:
        entity["number_of_seasons"] = count
    verdict = {"found": True, "clean_title": "季号边界测试", "content_type": "tv",
               "inferred_season": inferred, "inferred_episode": 10, "matched_entity": entity}
    # Run the actual graph's source tool before finalize, so this test reaches
    # the season validator with a grounded identity. Source data are synthetic.
    source = AsyncMock(return_value={"success": True, "data": [{
        "external_id": entity["external_id"], "content_type": "tv",
        "number_of_seasons": count,
    }]})
    monkeypatch.setattr("app.services.metadata_agent._execute_search_tmdb", source)
    model = ToolModel(responses=[
        AIMessage(content="", tool_calls=[{"name": "search_tmdb", "id": "season-source",
                                          "args": {"query": "季号边界测试"}}]),
        AIMessage(content="", tool_calls=[{"name": "finalize", "id": "season-verdict",
                                          "args": {"result_json": json.dumps(verdict)}}]),
        AIMessage(content="finished"),
    ])
    agent = UnifiedMetadataAgent()
    agent._model = model
    result = await agent.process(resource, channel, db_session, force_refresh=True)
    await db_session.commit()
    source.assert_awaited_once()
    assert result.found
    assert result.season == expected
    assert result.season_ambiguous == (expected is None)
    await db_session.refresh(resource)
    assert resource.season != 99
    works = (await db_session.execute(select(TVSeries))).scalars().all()
    assert all(work.season_number != 99 for work in works)
    if expected is not None:
        assert resource.series_id is not None
        assert (await db_session.get(TVSeries, resource.series_id)).season_number == expected
    else:
        assert resource.series_id is None
        assert not works
    cache = (await db_session.execute(select(MetadataCache))).scalar_one()
    assert cache.metadata_json["inferred_season"] == expected
    # Force another model run: a corrected/ambiguous result must stay stable.
    again = await agent.process(resource, channel, db_session, force_refresh=True)
    await db_session.commit()
    assert again.season == expected
    assert not (await db_session.execute(select(TVSeries).where(TVSeries.season_number == 99))).scalars().all()

    cached = await agent.process(resource, channel, db_session)
    await db_session.commit()
    assert cached.season == expected
    await db_session.refresh(resource)
    if expected is None:
        assert resource.series_id is None
        assert resource.episode_confidence == "ambiguous"
    assert not (await db_session.execute(select(TVSeries).where(TVSeries.season_number == 99))).scalars().all()
