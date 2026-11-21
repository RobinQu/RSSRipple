"""In-process integration coverage for the Wikipedia metadata client.

The client primitives are pure Python over HTTP; this module exercises them
with in-memory fakes so the distributed/single-node integration coverage gate
includes the wikipedia network layer without touching the public internet.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import metadata_wikipedia_client as mwc


class _FakePage:
    def __init__(
        self,
        title="T",
        pageid=1,
        fullurl="https://x/wiki/T",
        summary="summary",
        exists=True,
        categories=None,
        langlinks=None,
    ):
        self.title = title
        self.pageid = pageid
        self.fullurl = fullurl
        self.summary = summary
        self._exists = exists
        self.categories = categories if categories is not None else {}
        self.langlinks = langlinks if langlinks is not None else {}

    def exists(self):
        return self._exists


class _FakeWiki:
    def __init__(self, *, search_pages=None, search_exc=None, page_obj=None):
        self._search_pages = search_pages or {}
        self._search_exc = search_exc
        self._page_obj = page_obj

    def search(self, query, limit=8):
        if self._search_exc is not None:
            raise self._search_exc
        return SimpleNamespace(pages=self._search_pages)

    def page(self, title):
        return self._page_obj


class _Resp:
    def __init__(self, payload=None, status_code=200, exc=None):
        self._payload = payload
        self.status_code = status_code
        self._exc = exc

    def raise_for_status(self):
        if self._exc is not None:
            raise self._exc

    def json(self):
        return self._payload


class _HttpFactory:
    """Returns successive responses from a queued list for AsyncClient.get."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.urls: list[str] = []

    def __call__(self, *a, **kw):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, params=None, headers=None, **kw):
        self.urls.append(url)
        resp = self._responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp


def _patch_wiki(monkeypatch, wiki):
    monkeypatch.setattr(mwc, "_wikipedia_client", lambda lang: wiki)


def test_is_disambiguation_category_variants():
    assert mwc._is_disambiguation_category(["Disambiguation pages"]) is True
    assert mwc._is_disambiguation_category(["消歧义页面"]) is True
    assert mwc._is_disambiguation_category(["2020 anime"]) is False
    assert mwc._is_disambiguation_category([]) is False


async def test_wiki_call_normalizes_errors():
    def _ok(x):
        return x + 1

    assert await mwc._wiki_call(_ok, 1) == 2

    import wikipediaapi

    def _library_error():
        raise wikipediaapi.WikipediaException("server exploded")

    with pytest.raises(mwc._WikipediaRequestError):
        await mwc._wiki_call(_library_error)

    def _network_error():
        raise OSError("connection timed out")

    with pytest.raises(mwc._WikipediaRequestError) as exc:
        await mwc._wiki_call(_network_error)
    assert "network error" in str(exc.value)


async def test_execute_search_collects_existing_pages(monkeypatch):
    good = _FakePage(title="Good", pageid=2, summary="S" * 900)
    missing = _FakePage(title="Missing", exists=False)
    _patch_wiki(monkeypatch, _FakeWiki(search_pages={"Good": good, "Missing": missing}))
    out = await mwc._execute_search_wikipedia("q", "en")
    assert out["success"] is True
    assert [page["title"] for page in out["data"]] == ["Good"]
    assert len(out["data"][0]["summary"]) <= 500


async def test_fetch_page_image_original_and_rest_fallback(monkeypatch):
    import httpx

    factory = _HttpFactory([
        _Resp(payload={"query": {"pages": {"1": {"title": "T", "original": {"source": "orig"}}}}}),
    ])
    monkeypatch.setattr(httpx, "AsyncClient", factory)
    assert await mwc._fetch_wikipedia_page_image("T", "en", page_id=1) == "orig"

    factory = _HttpFactory([
        _Resp(payload={"query": {"pages": {}}}),
        _Resp(payload={"title": "T", "thumbnail": {"source": "rest-thumb"}}),
    ])
    monkeypatch.setattr(httpx, "AsyncClient", factory)
    assert await mwc._fetch_wikipedia_page_image("T", "en") == "rest-thumb"

    factory = _HttpFactory([
        _Resp(payload={"query": {"pages": {}}}),
        _Resp(status_code=404),
    ])
    monkeypatch.setattr(httpx, "AsyncClient", factory)
    assert await mwc._fetch_wikipedia_page_image("T", "en") is None


async def test_fetch_wikitext_and_langlink_pageids(monkeypatch):
    import httpx

    factory = _HttpFactory([
        _Resp(payload={"parse": {"wikitext": "WT"}}),
        _Resp(payload={"query": {"pages": {"1": {"pageid": 55}}}}),
        _Resp(payload={"query": {"pages": {"2": {"pageid": -1}}}}),
    ])
    monkeypatch.setattr(httpx, "AsyncClient", factory)
    assert await mwc.fetch_wikipedia_wikitext("Page", "zh") == "WT"
    langlink_ids = await mwc._fetch_langlink_pageids({"en": "Title", "zh": "标题"})
    assert langlink_ids == {"en": 55}


async def test_get_page_success_and_disambiguation(monkeypatch):
    page = _FakePage(
        title="作品",
        pageid=42,
        fullurl="https://zh.wikipedia.org/?curid=42",
        summary="简介",
        categories={"2024 anime television series": 1},
        langlinks={"en": SimpleNamespace(title="Work EN")},
    )
    _patch_wiki(monkeypatch, _FakeWiki(page_obj=page))
    monkeypatch.setattr(mwc, "_fetch_langlink_pageids", AsyncMock(return_value={"en": 77}))
    monkeypatch.setattr(mwc, "_fetch_wikipedia_page_image", AsyncMock(return_value="poster.jpg"))
    out = await mwc._execute_get_wikipedia_page("作品", "zh")
    assert out["success"] is True
    assert out["data"]["page_id"] == 42
    assert out["data"]["poster_url"] == "poster.jpg"
    assert out["data"]["langlinks"] == {"en": "Work EN"}
    assert out["data"]["langlink_pageids"] == {"en": 77}

    disambiguation = _FakePage(title="T", categories={"Disambiguation pages": 1})
    _patch_wiki(monkeypatch, _FakeWiki(page_obj=disambiguation))
    out = await mwc._execute_get_wikipedia_page("T", "en")
    assert out["success"] is True
    assert out["data"]["disambiguation"] is True
