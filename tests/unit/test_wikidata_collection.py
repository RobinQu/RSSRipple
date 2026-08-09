"""Tests for wikidata_collection: deterministic TV franchise grouping via P179.

httpx is mocked at ``httpx.AsyncClient`` level (same pattern as
test_collection_service.py); the mock routes on the request params
(action/ids) so one client serves pageprops / wbgetentities / wbsearchentities.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import select

from app.models.series import TVSeries
from app.models.work_collection import WorkCollection
from app.services import wikidata_collection as wc


def _uuid() -> str:
    return str(uuid.uuid4())


def _claim(qid: str) -> dict:
    return {
        "mainsnak": {
            "snaktype": "value",
            "datavalue": {"value": {"entity-type": "item", "id": qid}},
        }
    }


def _client_mock(handler):
    """handler(url, params) -> payload dict; wrapped as an httpx.AsyncClient mock."""

    def _resp(payload: dict) -> MagicMock:
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = payload
        return resp

    client = MagicMock()

    async def _get(url, params=None):
        return _resp(handler(url, params or {}))

    client.get = AsyncMock(side_effect=_get)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=client)


def _series(**kwargs) -> TVSeries:
    return TVSeries(id=_uuid(), content_type="tv", **kwargs)


# ---------------------------------------------------------------------------
# extract_p179_qids (pure)
# ---------------------------------------------------------------------------


class TestExtractP179:
    def test_single_value(self):
        entity = {"claims": {"P179": [_claim("Q200")]}}
        assert wc.extract_p179_qids(entity) == ["Q200"]

    def test_absent(self):
        assert wc.extract_p179_qids({"claims": {}}) == []
        assert wc.extract_p179_qids({}) == []

    def test_multiple_values_kept_distinct(self):
        entity = {"claims": {"P179": [_claim("Q200"), _claim("Q201"), _claim("Q200")]}}
        assert wc.extract_p179_qids(entity) == ["Q200", "Q201"]

    def test_malformed_claims_skipped(self):
        entity = {
            "claims": {
                "P179": [
                    {"mainsnak": {"snaktype": "novalue"}},
                    {"mainsnak": {"snaktype": "value"}},  # no datavalue
                    _claim("Q200"),
                ]
            }
        }
        assert wc.extract_p179_qids(entity) == ["Q200"]


# ---------------------------------------------------------------------------
# Entity resolution (httpx mocked)
# ---------------------------------------------------------------------------


class TestResolveEntity:
    async def test_wikipedia_url_resolves_qid(self):
        def handler(url, params):
            assert url == "https://en.wikipedia.org/w/api.php"
            assert params["titles"] == "Ghost in the Shell"
            return {"query": {"pages": [{"pageprops": {"wikibase_item": "Q100"}}]}}

        with patch("httpx.AsyncClient", _client_mock(handler)):
            qid = await wc.resolve_qid_from_wikipedia_url(
                "https://en.wikipedia.org/wiki/Ghost_in_the_Shell"
            )
        assert qid == "Q100"

    async def test_wikipedia_url_missing_pageprops(self):
        def handler(url, params):
            return {"query": {"pages": [{"missing": True}]}}

        with patch("httpx.AsyncClient", _client_mock(handler)):
            assert await wc.resolve_qid_from_wikipedia_url(
                "https://zh.wikipedia.org/wiki/不存在的页面"
            ) is None

    async def test_unparseable_url(self):
        assert await wc.resolve_qid_from_wikipedia_url("https://example.com/x") is None

    async def test_search_exact_match_only(self):
        def handler(url, params):
            assert params["action"] == "wbsearchentities"
            return {
                "search": [
                    {"id": "Q100", "label": "Ghost in the Shell"},
                    {"id": "Q101", "label": "Ghost in the Shell 2: Innocence"},
                ]
            }

        with patch("httpx.AsyncClient", _client_mock(handler)):
            assert await wc.search_entity_qid(["Ghost in the Shell"]) == "Q100"

    async def test_search_no_exact_match_skips(self):
        """Precision over recall: fuzzy-but-not-exact labels resolve to skip."""

        def handler(url, params):
            return {"search": [{"id": "Q100", "label": "Ghost in the Shell 2"}]}

        with patch("httpx.AsyncClient", _client_mock(handler)):
            assert await wc.search_entity_qid(["Ghost in the Shell"]) is None

    async def test_search_ambiguous_exact_matches_skip(self):
        def handler(url, params):
            return {
                "search": [
                    {"id": "Q100", "label": "Duplicate"},
                    {"id": "Q101", "label": "duplicate"},  # case-insensitive exact
                ]
            }

        with patch("httpx.AsyncClient", _client_mock(handler)):
            assert await wc.search_entity_qid(["Duplicate"]) is None

    async def test_search_alias_counts_as_exact_match(self):
        def handler(url, params):
            return {
                "search": [{"id": "Q100", "label": "Other", "aliases": ["攻壳机动队"]}]
            }

        with patch("httpx.AsyncClient", _client_mock(handler)):
            assert await wc.search_entity_qid(["攻壳机动队"]) == "Q100"


# ---------------------------------------------------------------------------
# Upsert convergence + link flow (db)
# ---------------------------------------------------------------------------


def _pageprops_handler(work_qid: str, p179_qids: list[str], franchise_labels: dict):
    def handler(url, params):
        action = params.get("action")
        if action == "query":
            return {"query": {"pages": [{"pageprops": {"wikibase_item": work_qid}}]}}
        if action == "wbgetentities":
            if params["ids"] == work_qid:
                return {
                    "entities": {
                        work_qid: {"id": work_qid, "claims": {"P179": [_claim(q) for q in p179_qids]}}
                    }
                }
            return {"entities": {params["ids"]: {"id": params["ids"], "labels": franchise_labels}}}
        return {}

    return handler


_LABELS = {
    "zh": {"value": "攻壳机动队（系列）"},
    "en": {"value": "Ghost in the Shell franchise"},
}


class TestUpsertConvergence:
    async def test_same_qid_converges_to_one_row(self, db_session):
        c1 = await wc.upsert_collection_from_wikidata(
            db_session, "Q200", "攻壳机动队（系列）", "Ghost in the Shell franchise"
        )
        c2 = await wc.upsert_collection_from_wikidata(db_session, "Q200", "攻壳机动队（系列）", None)
        assert c1.id == c2.id
        rows = (await db_session.execute(select(WorkCollection))).scalars().all()
        assert len(rows) == 1
        assert rows[0].external_source == "wikidata"
        assert rows[0].external_id == "Q200"
        assert rows[0].title_en == "Ghost in the Shell franchise"

    async def test_labels_fallback_for_title_cn(self, db_session):
        coll = await wc.upsert_collection_from_wikidata(db_session, "Q300", None, "En Only")
        assert coll.title_cn == "En Only"  # title_cn is non-nullable
        coll2 = await wc.upsert_collection_from_wikidata(db_session, "Q301", None, None)
        assert coll2.title_cn == "Wikidata Q301"


class TestLinkSeries:
    async def test_happy_path_via_wikipedia_url(self, db_session):
        series = _series(
            title_en="Ghost in the Shell: SAC_2045",
            wikipedia_url="https://en.wikipedia.org/wiki/Ghost_in_the_Shell:_SAC_2045",
        )
        db_session.add(series)
        await db_session.flush()

        with patch(
            "httpx.AsyncClient", _client_mock(_pageprops_handler("Q100", ["Q200"], _LABELS))
        ):
            status = await wc.link_series_wikidata_collection(db_session, series)

        assert status == wc.STATUS_LINKED
        assert series.collection_id is not None
        coll = await db_session.get(WorkCollection, series.collection_id)
        assert coll.external_source == "wikidata"
        assert coll.external_id == "Q200"
        assert coll.title_cn == "攻壳机动队（系列）"

    async def test_same_franchise_series_converge(self, db_session):
        s1 = _series(title_en="SAC", wikipedia_url="https://en.wikipedia.org/wiki/SAC")
        s2 = _series(title_en="SAC 2nd", wikipedia_url="https://en.wikipedia.org/wiki/SAC_2nd")
        db_session.add_all([s1, s2])
        await db_session.flush()

        with patch(
            "httpx.AsyncClient", _client_mock(_pageprops_handler("Q100", ["Q200"], _LABELS))
        ):
            assert await wc.link_series_wikidata_collection(db_session, s1) == wc.STATUS_LINKED
            assert await wc.link_series_wikidata_collection(db_session, s2) == wc.STATUS_LINKED
        assert s1.collection_id == s2.collection_id
        rows = (await db_session.execute(select(WorkCollection))).scalars().all()
        assert len(rows) == 1

    async def test_no_p179(self, db_session):
        series = _series(title_en="X", wikipedia_url="https://en.wikipedia.org/wiki/X")
        db_session.add(series)
        await db_session.flush()

        with patch(
            "httpx.AsyncClient", _client_mock(_pageprops_handler("Q100", [], _LABELS))
        ):
            status = await wc.link_series_wikidata_collection(db_session, series)
        assert status == wc.STATUS_NO_P179
        assert series.collection_id is None

    async def test_multiple_p179_is_ambiguous_skip(self, db_session):
        series = _series(title_en="X", wikipedia_url="https://en.wikipedia.org/wiki/X")
        db_session.add(series)
        await db_session.flush()

        with patch(
            "httpx.AsyncClient",
            _client_mock(_pageprops_handler("Q100", ["Q200", "Q201"], _LABELS)),
        ):
            status = await wc.link_series_wikidata_collection(db_session, series)
        assert status == wc.STATUS_AMBIGUOUS
        assert series.collection_id is None
        rows = (await db_session.execute(select(WorkCollection))).scalars().all()
        assert rows == []

    async def test_no_entity(self, db_session):
        def handler(url, params):
            if params.get("action") == "wbsearchentities":
                return {"search": []}
            return {}

        series = _series(title_en="Totally Unknown Work")
        db_session.add(series)
        await db_session.flush()

        with patch("httpx.AsyncClient", _client_mock(handler)):
            status = await wc.link_series_wikidata_collection(db_session, series)
        assert status == wc.STATUS_NO_ENTITY
        assert series.collection_id is None

    async def test_dry_run_writes_nothing(self, db_session):
        series = _series(title_en="X", wikipedia_url="https://en.wikipedia.org/wiki/X")
        db_session.add(series)
        await db_session.flush()

        with patch(
            "httpx.AsyncClient", _client_mock(_pageprops_handler("Q100", ["Q200"], _LABELS))
        ):
            status = await wc.link_series_wikidata_collection(db_session, series, apply=False)
        assert status == wc.STATUS_LINKED
        assert series.collection_id is None
        rows = (await db_session.execute(select(WorkCollection))).scalars().all()
        assert rows == []

    async def test_already_linked_is_noop(self, db_session):
        coll = WorkCollection(id=_uuid(), title_cn="已有合集")
        series = _series(title_en="X", collection_id=coll.id)
        db_session.add_all([coll, series])
        await db_session.flush()

        with patch("httpx.AsyncClient") as client_cls:
            status = await wc.link_series_wikidata_collection(db_session, series)
        assert status == wc.STATUS_ALREADY_LINKED
        client_cls.assert_not_called()
