"""Tests for metadata_exa_fallback: URL id extraction, Exa MCP search wrapper,
LLM judge, and the public exa_fallback_judge entry point.

No network: the Exa MCP client and the LLM model are mocked.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.services.metadata_exa_fallback import (
    _build_exa_evidence_text,
    _exa_judge,
    _exa_web_search,
    _guess_url_from_description,
    _source_and_id_from_url,
    exa_fallback_judge,
)

# ---------------------------------------------------------------------------
# _source_and_id_from_url
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://bangumi.tv/subject/12345", ("bangumi", "bangumi:12345")),
        ("https://bgm.tv/subject/7", ("bangumi", "bangumi:7")),
        ("https://www.themoviedb.org/tv/999", ("tmdb", "tmdb:999")),
        ("https://myanimelist.net/anime/5114", ("mal", "mal:5114")),
        ("https://anilist.co/anime/21", ("anilist", "anilist:21")),
        ("https://www.imdb.com/title/tt0944947/", ("imdb", "imdb:tt0944947")),
        ("https://movie.douban.com/subject/1292052", ("douban", "douban:1292052")),
    ],
)
def test_source_and_id_from_authoritative_urls(url, expected):
    assert _source_and_id_from_url(url) == expected


def test_dropped_sites_fall_back_to_exa_web():
    # baidu_baike / eiga are dropped from the identity scheme (Phase P1).
    assert _source_and_id_from_url("https://baike.baidu.com/item/测试剧集/12345") == ("exa_web", None)
    assert _source_and_id_from_url("https://eiga.com/movie/12345") == ("exa_web", None)


def test_unrecognized_or_idless_urls_fall_back_to_exa_web():
    assert _source_and_id_from_url("https://example.com/page") == ("exa_web", None)
    # Known host but no id pattern in path
    assert _source_and_id_from_url("https://bangumi.tv/") == ("exa_web", None)


# ---------------------------------------------------------------------------
# _guess_url_from_description / _build_exa_evidence_text
# ---------------------------------------------------------------------------


def test_guess_url_from_description():
    assert _guess_url_from_description("see https://example.com/x) for more") == "https://example.com/x"
    assert _guess_url_from_description("no link here") == ""
    assert _guess_url_from_description("") == ""


def test_build_exa_evidence_text_format():
    hits = [
        {
            "title": "T1",
            "url": "https://bangumi.tv/subject/1",
            "external_source": "bangumi",
            "external_id": "bangumi:1",
            "text": "line1\nline2",
        },
        {"title": "T2", "url": "https://a.com", "source_domain": "a.com", "text": None},
    ]
    text = _build_exa_evidence_text(hits)
    assert "[1] title=T1" in text
    assert "source=bangumi (canonical id: bangumi:1)" in text
    assert "line1 line2" in text  # newlines flattened
    assert "[2] title=T2" in text
    assert "source=a.com" in text  # falls back to source_domain


# ---------------------------------------------------------------------------
# _exa_web_search
# ---------------------------------------------------------------------------


async def test_exa_web_search_returns_empty_when_disabled():
    with patch.dict(
        "app.services.runtime_config._overrides",
        {"exa_enabled": "false"},
    ):
        assert await _exa_web_search("anything") == []


async def test_exa_web_search_normalizes_results():
    raw = [
        {"url": "https://bangumi.tv/subject/42", "title": "Work", "text": "desc"},
        {"url": "https://example.com/p", "title": "Doc", "text": "t"},
    ]
    with (
        patch.dict(
            "app.services.runtime_config._overrides",
            {"exa_enabled": "true"},
        ),
        patch("app.services.exa_mcp_client.web_search", new=AsyncMock(return_value=raw)),
    ):
        hits = await _exa_web_search("query")

    assert len(hits) == 2
    assert hits[0]["external_source"] == "bangumi"
    assert hits[0]["external_id"] == "bangumi:42"
    assert hits[0]["source_domain"] == "bangumi.tv"
    # unknown host -> exa_web
    assert hits[1]["external_source"] == "exa_web"
    assert hits[1]["external_id"] is None


async def test_exa_web_search_reraises_search_errors():
    with (
        patch.dict(
            "app.services.runtime_config._overrides",
            {"exa_enabled": "true"},
        ),
        patch(
            "app.services.exa_mcp_client.web_search",
            new=AsyncMock(side_effect=RuntimeError("rate limited")),
        ),
        pytest.raises(RuntimeError, match="rate limited"),
    ):
        await _exa_web_search("query")


# ---------------------------------------------------------------------------
# _exa_judge
# ---------------------------------------------------------------------------


_HITS = [{
    "title": "T",
    "url": "https://bangumi.tv/subject/1",
    "text": "x",
    "external_source": "bangumi",
    "external_id": "bangumi:1",
}]


async def test_exa_judge_returns_none_without_hits():
    assert await _exa_judge(AsyncMock(), "raw", []) is None


async def test_exa_judge_parses_finalize_json():
    model = AsyncMock()
    model.ainvoke.return_value = SimpleNamespace(
        content='```json\n{"found": true, "clean_title": "X"}\n```'
    )
    out = await _exa_judge(model, "raw title", _HITS)
    assert out == {"found": True, "clean_title": "X"}


async def test_exa_judge_handles_structured_content_and_resource_hints():
    model = AsyncMock()
    model.ainvoke.return_value = SimpleNamespace(
        content=[SimpleNamespace(text='{"found": false}')]
    )
    resource = SimpleNamespace(title_cn="标题", title_en="Title", episode=3, season=2)
    out = await _exa_judge(model, "raw title", _HITS, resource=resource)
    assert out == {"found": False}
    user_msg = model.ainvoke.call_args[0][0][1].content
    assert "title_cn='标题'" in user_msg
    assert "episode=3" in user_msg


async def test_exa_judge_llm_failure_returns_none():
    model = AsyncMock()
    model.ainvoke.side_effect = RuntimeError("llm down")
    assert await _exa_judge(model, "raw title", _HITS) is None


async def test_exa_judge_unparseable_json_returns_none():
    model = AsyncMock()
    model.ainvoke.return_value = SimpleNamespace(content="no json at all")
    assert await _exa_judge(model, "raw title", _HITS) is None


# ---------------------------------------------------------------------------
# exa_fallback_judge
# ---------------------------------------------------------------------------


async def test_fallback_returns_none_when_exa_disabled():
    with patch.dict(
        "app.services.runtime_config._overrides",
        {"exa_enabled": "false"},
    ):
        assert await exa_fallback_judge(AsyncMock(), "title") is None


async def test_fallback_search_failure_is_transient():
    searcher = AsyncMock(side_effect=RuntimeError("network"))
    finalize, info = await exa_fallback_judge(AsyncMock(), "Some Title", exa_searcher=searcher)
    assert finalize["found"] is False
    assert "network" in info["error"]
    assert info["source_errors"]["exa"] == info["error"]
    assert info["method"] == "search_then_exa_fallback"


async def test_fallback_no_hits_is_definitive_not_found():
    searcher = AsyncMock(return_value=[])
    finalize, info = await exa_fallback_judge(AsyncMock(), "Some Title", exa_searcher=searcher)
    assert finalize["found"] is False
    assert finalize["reason"] == "no credible match in Exa web search"
    assert info["error"] is None


async def test_fallback_unparseable_judge_json_is_transient():
    searcher = AsyncMock(return_value=_HITS)
    model = AsyncMock()
    model.ainvoke.return_value = SimpleNamespace(content="garbage")
    finalize, info = await exa_fallback_judge(model, "Some Title", exa_searcher=searcher)
    assert finalize["found"] is False
    assert info["error"] == "exa judge returned unparseable JSON"


async def test_fallback_found_parses_id_from_matched_entity_url():
    searcher = AsyncMock(return_value=_HITS)
    model = AsyncMock()
    model.ainvoke.return_value = SimpleNamespace(content=(
        '{"found": true, "matched_entity": '
        '{"wikipedia_url": "https://bangumi.tv/subject/99", "title_cn": "作品"}}'
    ))
    finalize, info = await exa_fallback_judge(model, "作品", exa_searcher=searcher)
    me = finalize["matched_entity"]
    assert me["external_source"] == "bangumi"
    assert me["external_id"] == "bangumi:99"
    # defaults filled
    assert finalize["clean_title"] == "作品"
    assert finalize["content_type"] == "tv"
    assert info["error"] is None


async def test_fallback_entity_url_from_description():
    searcher = AsyncMock(return_value=_HITS)
    model = AsyncMock()
    model.ainvoke.return_value = SimpleNamespace(content=(
        '{"found": true, "matched_entity": '
        '{"description": "details at https://www.themoviedb.org/tv/55 end"}}'
    ))
    finalize, _ = await exa_fallback_judge(model, "Show", exa_searcher=searcher)
    me = finalize["matched_entity"]
    assert me["external_source"] == "tmdb"
    assert me["external_id"] == "tmdb:55"


async def test_fallback_entity_without_url_keeps_exa_web_source():
    searcher = AsyncMock(return_value=_HITS)
    model = AsyncMock()
    model.ainvoke.return_value = SimpleNamespace(
        content='{"found": true, "matched_entity": {"title_cn": "X"}}'
    )
    finalize, _ = await exa_fallback_judge(model, "X", exa_searcher=searcher)
    assert finalize["matched_entity"]["external_source"] == "exa_web"


async def test_fallback_query_carries_language_context():
    # Language context is only appended when a resource is provided.
    searcher = AsyncMock(return_value=[])
    await exa_fallback_judge(
        AsyncMock(), "测试标题", resource=SimpleNamespace(), exa_searcher=searcher
    )
    assert searcher.await_args[0][0] == "测试标题 动漫 改编"

    searcher.reset_mock()
    await exa_fallback_judge(
        AsyncMock(), "テストタイトル", resource=SimpleNamespace(), exa_searcher=searcher
    )
    assert searcher.await_args[0][0] == "テストタイトル アニメ ドラマ"

    searcher.reset_mock()
    await exa_fallback_judge(AsyncMock(), "Plain Title", exa_searcher=searcher)
    assert searcher.await_args[0][0] == "Plain Title"


# ---------------------------------------------------------------------------
# Ordered fallback whitelist + identity-only matched entity (Phase P1)
# ---------------------------------------------------------------------------

_ORDERED_HITS = [
    {"title": "D", "url": "https://movie.douban.com/subject/1", "text": "d",
     "external_source": "douban", "external_id": "douban:1"},
    {"title": "B", "url": "https://bangumi.tv/subject/2", "text": "b",
     "external_source": "bangumi", "external_id": "bangumi:2"},
    {"title": "W", "url": "https://example.com/blog", "text": "w",
     "external_source": "exa_web", "external_id": None},
]


async def test_fallback_empty_whitelist_disables_fallback():
    searcher = AsyncMock(return_value=_ORDERED_HITS)
    assert await exa_fallback_judge(
        AsyncMock(), "title", exa_searcher=searcher, fallback_sources=[]
    ) is None
    searcher.assert_not_awaited()


async def test_fallback_whitelist_filters_and_orders_hits():
    searcher = AsyncMock(return_value=list(_ORDERED_HITS))
    model = AsyncMock()
    model.ainvoke.return_value = SimpleNamespace(content='{"found": false}')
    await exa_fallback_judge(
        model, "title", exa_searcher=searcher, fallback_sources=["bangumi", "douban"]
    )
    user_msg = model.ainvoke.call_args[0][0][1].content
    # exa_web hit dropped (not a whitelist member); bangumi presented first
    # because it is earlier in the ordered whitelist.
    assert "example.com" not in user_msg
    assert user_msg.index("bangumi.tv") < user_msg.index("douban.com")


async def test_fallback_whitelist_without_match_is_definitive_not_found():
    hits = [h for h in _ORDERED_HITS if h["external_source"] == "douban"]
    searcher = AsyncMock(return_value=hits)
    finalize, info = await exa_fallback_judge(
        AsyncMock(), "title", exa_searcher=searcher, fallback_sources=["bangumi"]
    )
    assert finalize["found"] is False
    assert info["error"] is None


async def test_fallback_matched_entity_is_identity_only():
    searcher = AsyncMock(return_value=_HITS)
    model = AsyncMock()
    model.ainvoke.return_value = SimpleNamespace(content=(
        '{"found": true, "matched_entity": {"title_cn": "作品", '
        '"wikipedia_url": "https://bangumi.tv/subject/9", '
        '"number_of_seasons": 2, "number_of_episodes": 24, '
        '"seasons": [{"season_number": 1, "episode_count": 12}]}}'
    ))
    finalize, _ = await exa_fallback_judge(model, "作品", exa_searcher=searcher)
    me = finalize["matched_entity"]
    # Content follows the primary source: fallback-supplied entity must not
    # carry season/episode counts even when the LLM emits them.
    assert "number_of_seasons" not in me
    assert "number_of_episodes" not in me
    assert "seasons" not in me
    assert me["external_source"] == "bangumi"
    assert me["external_id"] == "bangumi:9"
