"""Tests for ``metadata_wikipedia_client``: the wikipediaapi wrapper, the
thread-bridge error normalization, search/page/image primitives and the
langlink-pageid resolver. All network surfaces are patched in-process."""

from __future__ import annotations

import sys
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


# ---------------------------------------------------------------------------
# _wikipedia_client + _is_disambiguation_category
# ---------------------------------------------------------------------------


def test_wikipedia_client_builds_client():
    import wikipediaapi

    mwc._wikipedia_client.cache_clear()
    client = mwc._wikipedia_client("zh")
    assert isinstance(client, wikipediaapi.Wikipedia)
    assert mwc._wikipedia_client("zh") is client  # lru_cache
    mwc._wikipedia_client.cache_clear()


def test_is_disambiguation_category_variants():
    assert mwc._is_disambiguation_category(["Disambiguation pages"]) is True
    assert mwc._is_disambiguation_category(["消歧义页面"]) is True
    assert mwc._is_disambiguation_category(["曖昧さ回避"]) is True
    assert mwc._is_disambiguation_category(["2020 anime"]) is False
    assert mwc._is_disambiguation_category([]) is False


# ---------------------------------------------------------------------------
# _wiki_call error normalization
# ---------------------------------------------------------------------------


async def test_wiki_call_success_and_library_exception():
    import wikipediaapi

    def _ok(x):
        return x + 1

    assert await mwc._wiki_call(_ok, 1) == 2

    def _boom():
        raise wikipediaapi.WikipediaException("server exploded")

    with pytest.raises(mwc._WikipediaRequestError) as ei:
        await mwc._wiki_call(_boom)
    assert str(ei.value).startswith("Wikipedia request failed")
    assert "WikipediaException" in str(ei.value)


async def test_wiki_call_network_error_and_generic():
    def _timeout():
        raise OSError("connection timed out")

    with pytest.raises(mwc._WikipediaRequestError) as ei:
        await mwc._wiki_call(_timeout)
    assert "network error" in str(ei.value)

    def _generic():
        raise ValueError("weird")

    with pytest.raises(mwc._WikipediaRequestError) as ei:
        await mwc._wiki_call(_generic)
    assert "ValueError" in str(ei.value)


# ---------------------------------------------------------------------------
# _execute_search_wikipedia
# ---------------------------------------------------------------------------


async def test_execute_search_import_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "wikipediaapi", None)
    out = await mwc._execute_search_wikipedia("q", "en")
    assert out["success"] is False
    assert "not installed" in out["error"]


async def test_execute_search_request_error(monkeypatch):
    _patch_wiki(monkeypatch, _FakeWiki(search_exc=RuntimeError("bad")))
    out = await mwc._execute_search_wikipedia("q", "en")
    assert out["success"] is False
    assert "bad" in out["error"]


class _BoomSummaryPage:
    title = "Flaky"
    pageid = 3
    fullurl = "u"

    def exists(self):
        return True

    @property
    def summary(self):
        raise RuntimeError("summary down")


async def test_execute_search_empty(monkeypatch):
    _patch_wiki(monkeypatch, _FakeWiki(search_pages={}))
    out = await mwc._execute_search_wikipedia("q", "en")
    assert out == {"success": True, "data": []}


async def test_execute_search_collects_pages_and_survives_summary_failure(monkeypatch):
    good = _FakePage(title="Good", pageid=2, summary="S" * 900)
    missing = _FakePage(title="Missing", exists=False)
    # A page whose summary fetch raises → "" fallback (line 155-160).
    flaky = _BoomSummaryPage()
    _patch_wiki(
        monkeypatch,
        _FakeWiki(search_pages={"Good": good, "Missing": missing, "Flaky": flaky}),
    )
    out = await mwc._execute_search_wikipedia("q", "fr")  # bad lang → en

    assert out["success"] is True
    titles = [p["title"] for p in out["data"]]
    assert "Missing" not in titles
    flaky_entry = next(p for p in out["data"] if p["title"] == "Flaky")
    assert flaky_entry["summary"] == ""
    assert good.title == "Good"


# ---------------------------------------------------------------------------
# _fetch_wikipedia_page_image
# ---------------------------------------------------------------------------


async def test_fetch_image_pageimages_original(monkeypatch):
    import httpx

    factory = _HttpFactory([
        _Resp(payload={"query": {"pages": {"1": {"title": "T", "original": {"source": "orig"}}}}}),
    ])
    monkeypatch.setattr(httpx, "AsyncClient", factory)
    out = await mwc._fetch_wikipedia_page_image("T", "en", page_id=1)
    assert out == "orig"
    assert factory.urls[0].endswith("/w/api.php")


async def test_fetch_image_pageimages_thumbnail_and_titles_param(monkeypatch):
    import httpx

    factory = _HttpFactory([
        _Resp(payload={"query": {"pages": {"1": {"title": "T", "thumbnail": {"source": "thumb"}}}}}),
    ])
    monkeypatch.setattr(httpx, "AsyncClient", factory)
    out = await mwc._fetch_wikipedia_page_image("T", "en")
    assert out == "thumb"


async def test_fetch_image_pageimages_title_mismatch_falls_to_rest(monkeypatch):
    import httpx

    factory = _HttpFactory([
        # pageimages page title does not match expected → skip to REST
        _Resp(payload={"query": {"pages": {"1": {"title": "Wrong", "original": {"source": "bad"}}}}}),
        _Resp(payload={"title": "T", "originalimage": {"source": "rest-orig"}}),
    ])
    monkeypatch.setattr(httpx, "AsyncClient", factory)
    out = await mwc._fetch_wikipedia_page_image("T", "en", page_id=1, expected_title="T")
    assert out == "rest-orig"


async def test_fetch_image_rest_thumbnail_and_non_200(monkeypatch):
    import httpx

    factory = _HttpFactory([
        RuntimeError("pageimages down"),  # pageimages raises → REST
        _Resp(payload={"title": "T", "thumbnail": {"source": "rest-thumb"}}),
    ])
    monkeypatch.setattr(httpx, "AsyncClient", factory)
    assert await mwc._fetch_wikipedia_page_image("T", "en") == "rest-thumb"

    factory2 = _HttpFactory([
        _Resp(payload={"query": {"pages": {}}}),
        _Resp(status_code=404),
    ])
    monkeypatch.setattr(httpx, "AsyncClient", factory2)
    assert await mwc._fetch_wikipedia_page_image("T", "en") is None


async def test_fetch_image_rest_title_mismatch_and_exception(monkeypatch):
    import httpx

    factory = _HttpFactory([
        _Resp(payload={"query": {"pages": {}}}),
        _Resp(payload={"title": "Unrelated", "originalimage": {"source": "x"}}),
    ])
    monkeypatch.setattr(httpx, "AsyncClient", factory)
    assert await mwc._fetch_wikipedia_page_image(
        "T", "en", expected_title="Completely Different Name Here"
    ) is None

    factory2 = _HttpFactory([
        _Resp(payload={"query": {"pages": {}}}),
        RuntimeError("rest down"),
    ])
    monkeypatch.setattr(httpx, "AsyncClient", factory2)
    assert await mwc._fetch_wikipedia_page_image("T", "en") is None


async def test_fetch_image_invalid_lang_falls_back_en(monkeypatch):
    import httpx

    factory = _HttpFactory([
        _Resp(payload={"query": {"pages": {}}}),
        _Resp(payload={"title": "T", "originalimage": {"source": "e"}}),
    ])
    monkeypatch.setattr(httpx, "AsyncClient", factory)
    out = await mwc._fetch_wikipedia_page_image("T", "fr")
    assert out == "e"
    assert "en.wikipedia.org" in factory.urls[0]


# ---------------------------------------------------------------------------
# fetch_wikipedia_wikitext
# ---------------------------------------------------------------------------


async def test_fetch_wikitext_empty_and_missing_body(monkeypatch):
    assert await mwc.fetch_wikipedia_wikitext("") is None
    import httpx

    factory = _HttpFactory([_Resp(payload={"parse": {}})])
    monkeypatch.setattr(httpx, "AsyncClient", factory)
    assert await mwc.fetch_wikipedia_wikitext("Some Page", "zh") is None


async def test_fetch_wikitext_url_and_exception(monkeypatch):
    import httpx

    factory = _HttpFactory([_Resp(payload={"parse": {"wikitext": "WT"}})])
    monkeypatch.setattr(httpx, "AsyncClient", factory)
    out = await mwc.fetch_wikipedia_wikitext(
        "https://zh.wikipedia.org/wiki/%E9%BB%83%E6%B3%89%E4%BD%BF%E8%80%85", "zh"
    )
    assert out == "WT"

    factory2 = _HttpFactory([RuntimeError("boom")])
    monkeypatch.setattr(httpx, "AsyncClient", factory2)
    assert await mwc.fetch_wikipedia_wikitext("Page", "zh") is None


# ---------------------------------------------------------------------------
# _fetch_langlink_pageids
# ---------------------------------------------------------------------------


async def test_fetch_langlink_pageids_empty():
    assert await mwc._fetch_langlink_pageids({}) == {}


async def test_fetch_langlink_pageids_success_and_failures(monkeypatch):
    import httpx

    factory = _HttpFactory([
        _Resp(payload={"query": {"pages": {"1": {"pageid": 55}}}}),
        _Resp(payload={"query": {"pages": {"2": {"pageid": -1}}}}),  # invalid → None
        RuntimeError("down"),  # exception → None
    ])
    monkeypatch.setattr(httpx, "AsyncClient", factory)
    out = await mwc._fetch_langlink_pageids({"en": "Title", "zh": "标题", "ja": "題"})
    assert out == {"en": 55}


# ---------------------------------------------------------------------------
# _execute_get_wikipedia_page
# ---------------------------------------------------------------------------


async def test_execute_get_page_import_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "wikipediaapi", None)
    out = await mwc._execute_get_wikipedia_page("T", "en")
    assert out["success"] is False
    assert "not installed" in out["error"]


async def test_execute_get_page_not_found(monkeypatch):
    page = _FakePage(title="T", exists=False)
    _patch_wiki(monkeypatch, _FakeWiki(page_obj=page))
    out = await mwc._execute_get_wikipedia_page("T", "en")
    assert out["success"] is False
    assert "Page not found" in out["error"]


async def test_execute_get_page_exists_check_error(monkeypatch):
    page = _FakePage(title="T")

    async def _boom(func, *a, **kw):
        raise mwc._WikipediaRequestError("exists down")

    _patch_wiki(monkeypatch, _FakeWiki(page_obj=page))
    monkeypatch.setattr(mwc, "_wiki_call", _boom)
    out = await mwc._execute_get_wikipedia_page("T", "en")
    assert out["success"] is False
    assert "exists down" in out["error"]


async def test_execute_get_page_disambiguation(monkeypatch):
    page = _FakePage(title="T", categories={"Disambiguation pages": 1})
    _patch_wiki(monkeypatch, _FakeWiki(page_obj=page))
    out = await mwc._execute_get_wikipedia_page("T", "en")
    assert out["success"] is True
    assert out["data"]["disambiguation"] is True


async def test_execute_get_page_success_full(monkeypatch):
    page = _FakePage(
        title="作品",
        pageid=42,
        fullurl="https://zh.wikipedia.org/?curid=42",
        summary="简介",
        categories={"2024 anime television series": 1},
        langlinks={
            "en": SimpleNamespace(title="Work EN"),
            "fr": SimpleNamespace(title="Oeuvre"),  # unsupported lang dropped
        },
    )
    _patch_wiki(monkeypatch, _FakeWiki(page_obj=page))
    monkeypatch.setattr(
        mwc, "_fetch_langlink_pageids", AsyncMock(return_value={"en": 77})
    )
    monkeypatch.setattr(
        mwc, "_fetch_wikipedia_page_image", AsyncMock(return_value="poster.jpg")
    )
    out = await mwc._execute_get_wikipedia_page("作品", "zh")
    assert out["success"] is True
    data = out["data"]
    assert data["page_id"] == 42
    assert data["summary"] == "简介"
    assert data["poster_url"] == "poster.jpg"
    assert data["langlinks"] == {"en": "Work EN"}
    assert data["langlink_pageids"] == {"en": 77}


async def test_execute_get_page_category_summary_langlink_failures(monkeypatch):
    page = _FakePage(title="T")
    _patch_wiki(monkeypatch, _FakeWiki(page_obj=page))
    monkeypatch.setattr(
        mwc, "_fetch_langlink_pageids", AsyncMock(return_value={})
    )
    monkeypatch.setattr(
        mwc, "_fetch_wikipedia_page_image", AsyncMock(return_value=None)
    )

    original = mwc._wiki_call
    calls = {"n": 0}

    async def _flaky(func, *args, **kwargs):
        name = getattr(func, "__name__", "")
        if name == "<lambda>":
            calls["n"] += 1
            # categories (1st) and summary (2nd) and langlinks (3rd)
            if calls["n"] == 1:
                raise mwc._WikipediaRequestError("categories down")
            if calls["n"] == 2:
                raise mwc._WikipediaRequestError("summary down")
            raise RuntimeError("langlinks down")
        return await original(func, *args, **kwargs)

    monkeypatch.setattr(mwc, "_wiki_call", _flaky)
    out = await mwc._execute_get_wikipedia_page("T", "en")
    assert out["success"] is True
    data = out["data"]
    assert data["categories"] == []
    assert data["summary"] == ""
    assert data["langlinks"] == {}
