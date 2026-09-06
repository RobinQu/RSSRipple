"""Coverage-oriented integration tests for app/services/wikidata_collection.py.

Complements the pure-helper tests in
tests/integration/test_metadata_core_integration.py by exercising the
network-shaped and DB-backed paths: ``_get_json`` failure handling, all three
QID resolvers, entity fetching, the idempotent collection upsert, and every
status branch of ``link_series_wikidata_collection``.

No real HTTP: ``_get_json`` / ``fetch_entity`` are mocked at module level, and
``_get_json`` itself is tested with a fake ``httpx.AsyncClient``.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy import select

from app.models.series import TVSeries
from app.models.work_collection import WorkCollection
from app.services import wikidata_collection as wc

MOD = "app.services.wikidata_collection"


def _series(**overrides):
    base = dict(
        title_en="Ghost in the Shell",
        original_title=None,
        title_cn="攻壳机动队",
        canonical_name=None,
        wikipedia_url=None,
        wikipedia_page_id=None,
        external_id=None,
        external_source="manual",
        collection_id=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


async def _persist_series(db_session, **overrides) -> TVSeries:
    s = TVSeries(
        id=str(uuid.uuid4()),
        title_cn="攻壳机动队",
        title_en="Ghost in the Shell",
        original_title="攻殻機動隊",
        external_id="tt-test-wikidata",
        external_source="manual",
        content_type="tv",
        **overrides,
    )
    db_session.add(s)
    await db_session.flush()
    return s


# ---------------------------------------------------------------------------
# _get_json (real implementation, fake httpx client)
# ---------------------------------------------------------------------------


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, payload=None, error=None):
        self._payload = payload
        self._error = error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url, params=None):
        if self._error is not None:
            raise self._error
        return _FakeResp(self._payload)


class TestGetJson:
    async def test_success(self):
        with patch(
            "httpx.AsyncClient",
            lambda **kw: _FakeClient(payload={"ok": True}),
        ):
            data = await wc._get_json("https://example.org/api", {"a": 1})
        assert data == {"ok": True}

    async def test_failure_returns_none(self):
        with patch(
            "httpx.AsyncClient",
            lambda **kw: _FakeClient(error=RuntimeError("boom")),
        ):
            assert await wc._get_json("https://example.org/api", {}) is None


# ---------------------------------------------------------------------------
# resolve_qid_from_wikipedia_url
# ---------------------------------------------------------------------------


class TestResolveFromUrl:
    async def test_invalid_url_short_circuits(self):
        get_json = AsyncMock()
        with patch(f"{MOD}._get_json", get_json):
            assert await wc.resolve_qid_from_wikipedia_url("not a url") is None
        get_json.assert_not_called()

    async def test_qid_from_pageprops(self):
        get_json = AsyncMock(
            return_value={"query": {"pages": [{"pageprops": {"wikibase_item": "Q42"}}]}}
        )
        with patch(f"{MOD}._get_json", get_json):
            qid = await wc.resolve_qid_from_wikipedia_url(
                "https://en.wikipedia.org/wiki/Ghost_in_the_Shell"
            )
        assert qid == "Q42"
        # The pinned host from the URL drives the API endpoint.
        assert get_json.await_args.args[0] == "https://en.wikipedia.org/w/api.php"

    async def test_no_pageprops_or_failure_returns_none(self):
        get_json = AsyncMock(return_value={"query": {"pages": [{"title": "X"}]}})
        with patch(f"{MOD}._get_json", get_json):
            assert (
                await wc.resolve_qid_from_wikipedia_url("https://en.wikipedia.org/wiki/X")
                is None
            )
        get_json = AsyncMock(return_value=None)
        with patch(f"{MOD}._get_json", get_json):
            assert (
                await wc.resolve_qid_from_wikipedia_url("https://en.wikipedia.org/wiki/X")
                is None
            )


# ---------------------------------------------------------------------------
# resolve_qid_from_page_id
# ---------------------------------------------------------------------------


class TestResolveFromPageId:
    def _pages(self, qid):
        return {"query": {"pages": [{"pageprops": {"wikibase_item": qid}}]}}

    async def test_first_host_without_qid_falls_through(self):
        async def fake_get(url, params):
            if "en.wikipedia.org" in url:
                return {"query": {"pages": [{"title": "missing"}]}}
            return self._pages("Q77")

        entity = {"labels": {"en": {"value": "Ghost in the Shell"}}}
        with patch(f"{MOD}._get_json", AsyncMock(side_effect=fake_get)), patch(
            f"{MOD}.fetch_entity", AsyncMock(return_value=entity)
        ):
            qid = await wc.resolve_qid_from_page_id(123, ["Ghost in the Shell"])
        assert qid == "Q77"

    async def test_label_mismatch_rejects_edition(self):
        entity = {"labels": {"en": {"value": "Something Else"}}}
        with patch(
            f"{MOD}._get_json", AsyncMock(return_value=self._pages("Q77"))
        ), patch(f"{MOD}.fetch_entity", AsyncMock(return_value=entity)):
            assert await wc.resolve_qid_from_page_id(123, ["攻壳机动队"]) is None

    async def test_entity_fetch_failure_skips_edition(self):
        with patch(
            f"{MOD}._get_json", AsyncMock(return_value=self._pages("Q77"))
        ), patch(f"{MOD}.fetch_entity", AsyncMock(return_value=None)):
            assert (
                await wc.resolve_qid_from_page_id(123, ["Ghost in the Shell"]) is None
            )


# ---------------------------------------------------------------------------
# search_entity_qid
# ---------------------------------------------------------------------------


class TestSearchEntityQid:
    async def test_single_exact_label_match(self):
        get_json = AsyncMock(
            return_value={
                "search": [
                    {"id": "Q9", "label": "Ghost in the Shell", "aliases": []},
                    {"id": "Q10", "label": "Ghost in the Shell 2", "aliases": []},
                ]
            }
        )
        with patch(f"{MOD}._get_json", get_json):
            assert await wc.search_entity_qid(["Ghost in the Shell"]) == "Q9"

    async def test_exact_match_via_alias(self):
        get_json = AsyncMock(
            return_value={"search": [{"id": "Q9", "label": "GitS", "aliases": ["攻壳机动队"]}]}
        )
        with patch(f"{MOD}._get_json", get_json) as p:
            assert await wc.search_entity_qid(["攻壳机动队"]) == "Q9"
        # CJK titles are searched with language=zh.
        assert p.await_args.kwargs["params"]["language"] == "zh"

    async def test_ambiguous_exact_matches_yield_none(self):
        get_json = AsyncMock(
            return_value={
                "search": [
                    {"id": "Q9", "label": "Mercury", "aliases": []},
                    {"id": "Q11", "label": "Mercury", "aliases": []},
                ]
            }
        )
        with patch(f"{MOD}._get_json", get_json):
            assert await wc.search_entity_qid(["Mercury"]) is None

    async def test_failed_or_empty_search_continues_to_next_title(self):
        calls = iter(
            [
                None,  # first title: request failed
                {"search": []},  # second title: no results
            ]
        )
        get_json = AsyncMock(side_effect=lambda *a, **kw: next(calls))
        with patch(f"{MOD}._get_json", get_json):
            assert await wc.search_entity_qid(["One", "Two"]) is None
        assert get_json.await_count == 2


# ---------------------------------------------------------------------------
# resolve_series_entity_qid
# ---------------------------------------------------------------------------


class TestResolveSeriesEntityQid:
    async def test_wikipedia_url_wins(self):
        s = _series(wikipedia_url="https://en.wikipedia.org/wiki/X", wikipedia_page_id=5)
        with patch(
            f"{MOD}.resolve_qid_from_wikipedia_url", AsyncMock(return_value="Q1")
        ) as by_url, patch(
            f"{MOD}.resolve_qid_from_page_id", AsyncMock(return_value="Q2")
        ) as by_pid, patch(
            f"{MOD}.search_entity_qid", AsyncMock(return_value="Q3")
        ) as by_search:
            assert await wc.resolve_series_entity_qid(s) == "Q1"
        by_url.assert_awaited_once()
        by_pid.assert_not_called()
        by_search.assert_not_called()

    async def test_page_id_fallback(self):
        s = _series(wikipedia_page_id=7301786)
        with patch(
            f"{MOD}.resolve_qid_from_page_id", AsyncMock(return_value="Q2")
        ) as by_pid:
            assert await wc.resolve_series_entity_qid(s) == "Q2"
        assert by_pid.await_args.args[0] == 7301786

    async def test_page_id_parsed_from_external_id(self):
        s = _series(external_id="wikipedia:zh:7301786")
        with patch(
            f"{MOD}.resolve_qid_from_page_id", AsyncMock(return_value="Q2")
        ) as by_pid:
            assert await wc.resolve_series_entity_qid(s) == "Q2"
        assert by_pid.await_args.args[0] == 7301786

    async def test_search_is_last_resort(self):
        s = _series(external_id="wikipedia:Some_Slug_Title")
        with patch(
            f"{MOD}.resolve_qid_from_wikipedia_url", AsyncMock(return_value=None)
        ), patch(
            f"{MOD}.resolve_qid_from_page_id", AsyncMock(return_value=None)
        ) as by_pid, patch(
            f"{MOD}.search_entity_qid", AsyncMock(return_value="Q3")
        ) as by_search:
            assert await wc.resolve_series_entity_qid(s) == "Q3"
        # Slug-form wikipedia ids carry no numeric page id.
        by_pid.assert_not_called()
        assert by_search.await_args.args[0] == ["Ghost in the Shell", "攻壳机动队"]


# ---------------------------------------------------------------------------
# fetch_entity
# ---------------------------------------------------------------------------


class TestFetchEntity:
    async def test_dict_entities_keyed_by_qid(self):
        payload = {"entities": {"Q42": {"id": "Q42", "labels": {}}}}
        with patch(f"{MOD}._get_json", AsyncMock(return_value=payload)):
            assert await wc.fetch_entity("Q42") == {"id": "Q42", "labels": {}}

    async def test_dict_entities_falls_back_to_first_value(self):
        payload = {"entities": {"QOTHER": {"id": "QOTHER"}}}
        with patch(f"{MOD}._get_json", AsyncMock(return_value=payload)):
            assert await wc.fetch_entity("Q42") == {"id": "QOTHER"}

    async def test_list_shaped_entities_tolerated(self):
        payload = {"entities": [{"id": "Q1"}]}
        with patch(f"{MOD}._get_json", AsyncMock(return_value=payload)):
            assert await wc.fetch_entity("Q1") == {"id": "Q1"}
        payload = {"entities": []}
        with patch(f"{MOD}._get_json", AsyncMock(return_value=payload)):
            assert await wc.fetch_entity("Q1") is None

    async def test_missing_or_failed_returns_none(self):
        payload = {"entities": {"Q42": {"missing": ""}}}
        with patch(f"{MOD}._get_json", AsyncMock(return_value=payload)):
            assert await wc.fetch_entity("Q42") is None
        with patch(f"{MOD}._get_json", AsyncMock(return_value=None)):
            assert await wc.fetch_entity("Q42") is None


# ---------------------------------------------------------------------------
# upsert_collection_from_wikidata (DB)
# ---------------------------------------------------------------------------


class TestUpsertCollection:
    async def test_create_then_idempotent_update(self, db_session):
        c1 = await wc.upsert_collection_from_wikidata(db_session, "Q900", "攻壳系列", None)
        assert c1.external_source == "wikidata"
        assert c1.external_id == "Q900"
        assert c1.title_cn == "攻壳系列"
        await db_session.commit()

        # Same key: returns the existing row, fills empty title_en, leaves a
        # different title_cn untouched per call semantics.
        c2 = await wc.upsert_collection_from_wikidata(
            db_session, "Q900", "攻壳系列", "Ghost franchise"
        )
        assert c2.id == c1.id
        assert c2.title_en == "Ghost franchise"

        # title_cn updates only when it actually differs.
        c3 = await wc.upsert_collection_from_wikidata(
            db_session, "Q900", "攻殻系列", "Ignored English"
        )
        assert c3.title_cn == "攻殻系列"
        assert c3.title_en == "Ghost franchise"

        rows = (
            (await db_session.execute(select(WorkCollection))).scalars().all()
        )
        assert len(rows) == 1

    async def test_title_fallbacks(self, db_session):
        c = await wc.upsert_collection_from_wikidata(db_session, "Q901", None, "English")
        assert c.title_cn == "English"
        c = await wc.upsert_collection_from_wikidata(db_session, "Q902", None, None)
        assert c.title_cn == "Wikidata Q902"


# ---------------------------------------------------------------------------
# link_series_wikidata_collection
# ---------------------------------------------------------------------------


def _entity_with_p179(*qids: str) -> dict:
    return {
        "claims": {
            "P179": [
                {"mainsnak": {"snaktype": "value", "datavalue": {"value": {"id": q}}}}
                for q in qids
            ]
        }
    }


class TestLinkSeries:
    async def test_already_linked(self, db_session):
        s = _series(collection_id="some-collection")
        assert await wc.link_series_wikidata_collection(db_session, s) == wc.STATUS_ALREADY_LINKED

    async def test_no_entity(self, db_session):
        with patch(f"{MOD}.resolve_series_entity_qid", AsyncMock(return_value=None)):
            status = await wc.link_series_wikidata_collection(db_session, _series())
        assert status == wc.STATUS_NO_ENTITY

    async def test_entity_fetch_failure(self, db_session):
        with patch(
            f"{MOD}.resolve_series_entity_qid", AsyncMock(return_value="Q1")
        ), patch(f"{MOD}.fetch_entity", AsyncMock(return_value=None)):
            status = await wc.link_series_wikidata_collection(db_session, _series())
        assert status == wc.STATUS_FAILED

    async def test_no_p179(self, db_session):
        with patch(
            f"{MOD}.resolve_series_entity_qid", AsyncMock(return_value="Q1")
        ), patch(f"{MOD}.fetch_entity", AsyncMock(return_value={"claims": {}})):
            status = await wc.link_series_wikidata_collection(db_session, _series())
        assert status == wc.STATUS_NO_P179

    async def test_ambiguous_multiple_p179(self, db_session):
        with patch(
            f"{MOD}.resolve_series_entity_qid", AsyncMock(return_value="Q1")
        ), patch(
            f"{MOD}.fetch_entity", AsyncMock(return_value=_entity_with_p179("Q7", "Q8"))
        ):
            status = await wc.link_series_wikidata_collection(db_session, _series())
        assert status == wc.STATUS_AMBIGUOUS

    async def test_franchise_fetch_failure(self, db_session):
        fetch = AsyncMock(side_effect=[_entity_with_p179("Q7"), None])
        with patch(
            f"{MOD}.resolve_series_entity_qid", AsyncMock(return_value="Q1")
        ), patch(f"{MOD}.fetch_entity", fetch):
            status = await wc.link_series_wikidata_collection(db_session, _series())
        assert status == wc.STATUS_FAILED

    async def test_dry_run_writes_nothing(self, db_session):
        franchise = {"labels": {"zh": {"value": "攻壳系列"}, "en": {"value": "GitS"}}}
        fetch = AsyncMock(side_effect=[_entity_with_p179("Q7"), franchise])
        s = await _persist_series(db_session)
        with patch(
            f"{MOD}.resolve_series_entity_qid", AsyncMock(return_value="Q1")
        ), patch(f"{MOD}.fetch_entity", fetch):
            status = await wc.link_series_wikidata_collection(db_session, s, apply=False)
        assert status == wc.STATUS_LINKED
        assert s.collection_id is None
        rows = (await db_session.execute(select(WorkCollection))).scalars().all()
        assert rows == []

    async def test_apply_links_and_upserts_collection(self, db_session):
        franchise = {"labels": {"zh": {"value": "攻壳系列"}, "en": {"value": "GitS"}}}
        fetch = AsyncMock(side_effect=[_entity_with_p179("Q7"), franchise])
        s = await _persist_series(db_session)
        with patch(
            f"{MOD}.resolve_series_entity_qid", AsyncMock(return_value="Q1")
        ), patch(f"{MOD}.fetch_entity", fetch):
            status = await wc.link_series_wikidata_collection(db_session, s)
        assert status == wc.STATUS_LINKED
        assert s.collection_id is not None
        collection = await db_session.get(WorkCollection, s.collection_id)
        assert collection.external_id == "Q7"
        assert collection.display_name == "攻壳系列"
