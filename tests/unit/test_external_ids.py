"""Tests for the Phase P3 identity bag (WorkExternalId + external_ids service).

Covers: bag add/idempotency/no-steal, reverse lookup (incl. wrong-type),
upsert convergence via the bag (cross-source), alt_external_ids bagging,
dedup-merge bag union, the seed migration, and source_links aggregation.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from unittest.mock import AsyncMock, patch

from sqlalchemy import select

from app.database import _apply_light_migrations
from app.models.movie import Movie
from app.models.series import TVSeries
from app.models.work_external_id import WorkExternalId
from app.services import metadata_service as ms
from app.services.external_ids import (
    add_external_id,
    find_work_by_external_id,
    list_external_ids,
    merge_external_id_bags,
)
from app.services.metadata_dedup import merge_duplicate_series
from app.services.metadata_source_registry import build_source_links


def _uuid() -> str:
    return str(uuid.uuid4())


def _series(**kw) -> TVSeries:
    defaults = dict(id=_uuid(), title_cn="剧集A", content_type="tv")
    defaults.update(kw)
    return TVSeries(**defaults)


async def _bag_pairs(db, work_type, work_id) -> set[tuple[str, str]]:
    rows = await list_external_ids(db, work_type, work_id)
    return {(r.source, r.external_id) for r in rows}


# ---------------------------------------------------------------------------
# add_external_id: add / idempotent / no-steal
# ---------------------------------------------------------------------------


async def test_add_external_id_canonicalizes_and_is_idempotent(db_session):
    s = _series()
    db_session.add(s)
    await db_session.flush()

    assert await add_external_id(db_session, "series", s.id, "tmdb", "TMDB 82684") is True
    # Canonical form stored (source + full "source:id").
    assert await _bag_pairs(db_session, "series", s.id) == {("tmdb", "tmdb:82684")}
    # Same id again (any shape) -> no-op.
    assert await add_external_id(db_session, "series", s.id, "tmdb", "tmdb:82684") is False
    assert await add_external_id(db_session, "series", s.id, "tmdb", "82684") is False
    rows = (await db_session.execute(select(WorkExternalId))).scalars().all()
    assert len(rows) == 1


async def test_add_external_id_skips_non_registry_and_empty(db_session):
    s = _series()
    db_session.add(s)
    await db_session.flush()

    assert await add_external_id(db_session, "series", s.id, "llm_search", "abc") is False
    assert await add_external_id(db_session, "series", s.id, "wikipedia", "") is False
    assert await add_external_id(db_session, "series", s.id, None, "wikipedia:1") is False
    assert await list_external_ids(db_session, "series", s.id) == []


async def test_add_external_id_never_steals_from_another_work(db_session):
    s1, s2 = _series(title_cn="剧集A"), _series(title_cn="剧集B")
    db_session.add_all([s1, s2])
    await db_session.flush()

    assert await add_external_id(db_session, "series", s1.id, "wikipedia", "wikipedia:1") is True
    # The id already maps to s1 - adding it for s2 must NOT re-point it.
    assert await add_external_id(db_session, "series", s2.id, "wikipedia", "wikipedia:1") is False
    row = (await db_session.execute(select(WorkExternalId))).scalar_one()
    assert row.work_id == s1.id
    assert await list_external_ids(db_session, "series", s2.id) == []


# ---------------------------------------------------------------------------
# find_work_by_external_id
# ---------------------------------------------------------------------------


async def test_find_work_by_external_id_roundtrip(db_session):
    s = _series()
    db_session.add(s)
    await db_session.flush()
    await add_external_id(db_session, "series", s.id, "wikipedia", "wikipedia:7727654")

    found = await find_work_by_external_id(db_session, "series", "wikipedia", "7727654")
    assert found is not None and found.id == s.id
    # Misses: unknown id, non-registry source, empty id.
    assert await find_work_by_external_id(db_session, "series", "wikipedia", "wikipedia:9") is None
    assert await find_work_by_external_id(db_session, "series", "llm_search", "x") is None
    assert await find_work_by_external_id(db_session, "series", "wikipedia", None) is None


async def test_find_work_by_external_id_ignores_wrong_type(db_session):
    """A bag hit for a work of the OTHER type must not leak across tables."""
    m = Movie(id=_uuid(), title_cn="电影A", content_type="movie")
    db_session.add(m)
    await db_session.flush()
    await add_external_id(db_session, "movie", m.id, "wikipedia", "wikipedia:5")

    assert await find_work_by_external_id(db_session, "series", "wikipedia", "wikipedia:5") is None
    found = await find_work_by_external_id(db_session, "movie", "wikipedia", "wikipedia:5")
    assert found is not None and found.id == m.id


# ---------------------------------------------------------------------------
# Upsert convergence via the bag (cross-source)
# ---------------------------------------------------------------------------


async def test_upsert_converges_cross_source_via_bag(db_session):
    """Work created via wikipedia:pid; a later tmdb:id upsert for the same work
    (title-converged once) bags the tmdb id; any subsequent fetch carrying only
    tmdb:id converges on the SAME row via the bag - no title luck involved."""
    poster = patch(
        "app.services.metadata_service.download_and_cache_poster",
        new_callable=AsyncMock, return_value=None,
    )
    with poster:
        s1 = await ms.create_or_update_series_from_external(db_session, {
            "external_id": "wikipedia:7727654", "external_source": "wikipedia",
            "title_cn": "黃泉使者", "content_type": "tv",
        })
        assert await _bag_pairs(db_session, "series", s1.id) == {
            ("wikipedia", "wikipedia:7727654")
        }

        # Exa-fallback shape: tmdb id, same title -> converges via title, bags tmdb id.
        s2 = await ms.create_or_update_series_from_external(db_session, {
            "external_id": "tmdb:82684", "external_source": "tmdb",
            "title_cn": "黃泉使者", "content_type": "tv",
        })
        assert s2.id == s1.id
        assert await _bag_pairs(db_session, "series", s1.id) == {
            ("wikipedia", "wikipedia:7727654"), ("tmdb", "tmdb:82684"),
        }

        # Later fetch: only the tmdb id, completely different title (would
        # previously spawn a duplicate row) -> bag hit, same row.
        s3 = await ms.create_or_update_series_from_external(db_session, {
            "external_id": "tmdb:82684", "external_source": "tmdb",
            "title_cn": "完全不同的标题", "content_type": "tv",
        })
        assert s3.id == s1.id

    all_series = (await db_session.execute(select(TVSeries))).scalars().all()
    assert len(all_series) == 1


async def test_upsert_bags_alt_external_ids(db_session):
    """alt_external_ids (e.g. wikipedia langlink pageids) join the bag at
    upsert; a later upsert carrying a langlink pageid converges via the bag."""
    poster = patch(
        "app.services.metadata_service.download_and_cache_poster",
        new_callable=AsyncMock, return_value=None,
    )
    with poster:
        s1 = await ms.create_or_update_series_from_external(db_session, {
            "external_id": "wikipedia:100", "external_source": "wikipedia",
            "title_cn": "某作品", "content_type": "tv",
            "alt_external_ids": [
                {"source": "wikipedia", "id": "wikipedia:200"},  # en page
                {"source": "wikipedia", "id": "wikipedia:300"},  # ja page
                {"source": "not_a_registry", "id": "x"},          # skipped
            ],
        })
        assert await _bag_pairs(db_session, "series", s1.id) == {
            ("wikipedia", "wikipedia:100"),
            ("wikipedia", "wikipedia:200"),
            ("wikipedia", "wikipedia:300"),
        }

        # The same work's EN page arrives as a fresh match (different title).
        s2 = await ms.create_or_update_series_from_external(db_session, {
            "external_id": "wikipedia:200", "external_source": "wikipedia",
            "title_en": "Some Work EN", "content_type": "tv",
        })
        assert s2.id == s1.id

    all_series = (await db_session.execute(select(TVSeries))).scalars().all()
    assert len(all_series) == 1


async def test_movie_upsert_converges_via_bag(db_session):
    poster = patch(
        "app.services.metadata_service.download_and_cache_poster",
        new_callable=AsyncMock, return_value=None,
    )
    with poster:
        m1 = await ms.create_or_update_movie_from_external(db_session, {
            "external_id": "wikipedia:55", "external_source": "wikipedia",
            "title_cn": "电影甲", "content_type": "movie",
        })
        m2 = await ms.create_or_update_movie_from_external(db_session, {
            "external_id": "bangumi:123", "external_source": "bangumi",
            "title_cn": "电影甲", "content_type": "movie",
        })
        assert m2.id == m1.id
        m3 = await ms.create_or_update_movie_from_external(db_session, {
            "external_id": "bangumi:123", "external_source": "bangumi",
            "title_cn": "别的名字", "content_type": "movie",
        })
        assert m3.id == m1.id
    assert len((await db_session.execute(select(Movie))).scalars().all()) == 1


# ---------------------------------------------------------------------------
# Dedup merge: bag union
# ---------------------------------------------------------------------------


async def test_dedup_merge_unions_bags(db_session):
    s1 = _series(title_cn="同名片", created_at=datetime(2024, 1, 1))
    s2 = _series(title_cn="同名片", created_at=datetime(2024, 1, 2))
    db_session.add_all([s1, s2])
    await db_session.flush()
    # s1 is the survivor (older). s2's bag ids AND its primary column id all
    # join the survivor's bag; the duplicate's rows are re-pointed.
    await add_external_id(db_session, "series", s1.id, "wikipedia", "wikipedia:1")
    await add_external_id(db_session, "series", s2.id, "tmdb", "tmdb:2")
    s2.external_id = "bangumi:99"
    s2.external_source = "bangumi"
    await db_session.flush()

    report = await merge_duplicate_series(db_session)
    assert report.series_removed == 1
    assert await _bag_pairs(db_session, "series", s1.id) == {
        ("wikipedia", "wikipedia:1"), ("tmdb", "tmdb:2"), ("bangumi", "bangumi:99"),
    }
    # No dangling rows for the deleted duplicate.
    assert await list_external_ids(db_session, "series", s2.id) == []


async def test_merge_external_id_bags_direct(db_session):
    s1, s2 = _series(title_cn="A"), _series(title_cn="B")
    db_session.add_all([s1, s2])
    await db_session.flush()
    await add_external_id(db_session, "series", s2.id, "mal", "mal:7")
    gained = await merge_external_id_bags(db_session, s1, [s2])
    assert gained == 1
    assert await _bag_pairs(db_session, "series", s1.id) == {("mal", "mal:7")}


# ---------------------------------------------------------------------------
# Seed migration
# ---------------------------------------------------------------------------


async def test_seed_migration_populates_bag_idempotently(db_engine, db_session):
    s = _series(external_id="wikipedia:7727654", external_source="wikipedia")
    m = Movie(
        id=_uuid(), title_cn="电影乙", content_type="movie",
        external_id="tmdb:42", external_source="tmdb",
    )
    legacy = _series(external_id="some-raw-id", external_source="llm_search")
    db_session.add_all([s, m, legacy])
    await db_session.commit()

    async with db_engine.begin() as conn:
        await _apply_light_migrations(conn)
        await _apply_light_migrations(conn)  # second run is a no-op

    rows = (await db_session.execute(select(WorkExternalId))).scalars().all()
    got = {(r.work_type, r.work_id, r.source, r.external_id) for r in rows}
    assert got == {
        ("series", s.id, "wikipedia", "wikipedia:7727654"),
        ("movie", m.id, "tmdb", "tmdb:42"),
        # llm_search is not a registry source -> not seeded.
    }


async def test_seed_migration_does_not_duplicate_upsert_bagged_ids(db_engine, db_session):
    """An id already bagged by the upsert path is not re-seeded."""
    poster = patch(
        "app.services.metadata_service.download_and_cache_poster",
        new_callable=AsyncMock, return_value=None,
    )
    with poster:
        s = await ms.create_or_update_series_from_external(db_session, {
            "external_id": "wikipedia:1", "external_source": "wikipedia",
            "title_cn": "剧集丙", "content_type": "tv",
        })
    await db_session.commit()

    async with db_engine.begin() as conn:
        await _apply_light_migrations(conn)

    rows = (await db_session.execute(
        select(WorkExternalId).where(WorkExternalId.source == "wikipedia")
    )).scalars().all()
    assert len(rows) == 1 and rows[0].work_id == s.id


# ---------------------------------------------------------------------------
# source_links aggregation (pure registry function)
# ---------------------------------------------------------------------------


def test_build_source_links_includes_bag_ids():
    links = build_source_links(
        "wikipedia:7727654", "wikipedia", "tv",
        extra_ids=["wikipedia:70545449", "tmdb:82684", "wikipedia:7727654"],
    )
    urls = {(link["source"], link["url"]) for link in links}
    assert urls == {
        ("wikipedia", "https://en.wikipedia.org/?curid=7727654"),
        ("wikipedia", "https://en.wikipedia.org/?curid=70545449"),
        ("tmdb", "https://www.themoviedb.org/tv/82684"),
    }  # the repeated primary id is deduped away


def test_build_source_links_extra_ids_ignore_garbage():
    links = build_source_links(
        "tmdb:1", "tmdb", "movie", extra_ids=["not-canonical", "unknownsrc:1", ""]
    )
    assert [(link["source"], link["url"]) for link in links] == [
        ("tmdb", "https://www.themoviedb.org/movie/1")
    ]


# ---------------------------------------------------------------------------
# langlinks pageids -> alt_external_ids (wiki judge wiring)
# ---------------------------------------------------------------------------


async def test_langlink_pageids_become_alt_external_ids_in_auto_link():
    from app.services import metadata_wiki_judge as wj

    _JUDGE = "app.services.metadata_wiki_judge"
    search = AsyncMock(return_value={
        "success": True,
        "data": [{"title": "無職転生", "page_id": 5, "url": "http://w/5", "summary": "s"}],
    })
    page = AsyncMock(return_value={"data": {
        "categories": ["2018年日本電視動畫"],
        "summary": "TV anime",
        "langlinks": {"en": "Mushoku Tensei", "ja": "無職転生"},
        "langlink_pageids": {"en": 70545449, "ja": 4433221},
    }})
    react_runner = AsyncMock()
    with (
        patch(f"{_JUDGE}._execute_search_wikipedia", search),
        patch(f"{_JUDGE}._execute_get_wikipedia_page", page),
        patch(f"{_JUDGE}.fetch_wikipedia_wikitext", AsyncMock(return_value=None)),
    ):
        finalize, info = await wj.run_search_then_judge(
            AsyncMock(), "無職転生",
            react_runner=react_runner,
            msg_builder=lambda raw, source: (raw, source),
        )
    assert info["method"] == "search_then_autolink"
    me = finalize["matched_entity"]
    assert me["external_id"] == "wikipedia:zh:5"
    assert {a["id"] for a in me["alt_external_ids"]} == {
        "wikipedia:en:70545449", "wikipedia:ja:4433221",
    }
    assert all(a["source"] == "wikipedia" for a in me["alt_external_ids"])


async def test_fetch_langlink_pageids_maps_titles_to_pageids():
    from app.services.metadata_wikipedia_client import _fetch_langlink_pageids

    class _Resp:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params=None, headers=None):
            lang = url.split("//")[1].split(".")[0]
            pid = {"en": 111, "ja": 222}[lang]
            return _Resp({"query": {"pages": {str(pid): {"pageid": pid}}}})

    import httpx

    orig = httpx.AsyncClient
    httpx.AsyncClient = _Client
    try:
        out = await _fetch_langlink_pageids({"en": "Show", "ja": "ショー"})
    finally:
        httpx.AsyncClient = orig
    assert out == {"en": 111, "ja": 222}
    # Empty input short-circuits without any HTTP.
    assert await _fetch_langlink_pageids({}) == {}


# ---------------------------------------------------------------------------
# Wikipedia dual storage forms (qualified wikipedia:{lang}:{pid} vs legacy
# bare wikipedia:{pid}) — cross-form lookup, in-place upgrade, pid dedup.
# ---------------------------------------------------------------------------


async def test_wikipedia_cross_form_lookup(db_session):
    s = _series()
    db_session.add(s)
    await db_session.flush()
    await add_external_id(db_session, "series", s.id, "wikipedia", "wikipedia:zh:7301786")

    # Qualified row is found by the legacy bare form and vice versa.
    found = await find_work_by_external_id(db_session, "series", "wikipedia", "wikipedia:7301786")
    assert found is not None and found.id == s.id
    found = await find_work_by_external_id(db_session, "series", "wikipedia", "wikipedia:zh:7301786")
    assert found is not None and found.id == s.id
    # A different EDITION with the same numeric pageid is a different page
    # entirely (pageids are per-edition) and must NOT converge.
    assert await find_work_by_external_id(
        db_session, "series", "wikipedia", "wikipedia:en:7301786"
    ) is None
    # A different pageid misses.
    assert await find_work_by_external_id(
        db_session, "series", "wikipedia", "wikipedia:ja:999"
    ) is None


async def test_wikipedia_bare_row_upgraded_by_qualified_add(db_session):
    s = _series()
    db_session.add(s)
    await db_session.flush()
    assert await add_external_id(db_session, "series", s.id, "wikipedia", "wikipedia:7301786") is True
    # Same pageid with the edition known upgrades the legacy row in place.
    assert await add_external_id(db_session, "series", s.id, "wikipedia", "wikipedia:zh:7301786") is False
    assert await _bag_pairs(db_session, "series", s.id) == {("wikipedia", "wikipedia:zh:7301786")}


async def test_wikipedia_pid_collision_never_steals(db_session):
    s1, s2 = _series(title_cn="剧集A"), _series(title_cn="剧集B")
    db_session.add_all([s1, s2])
    await db_session.flush()
    assert await add_external_id(db_session, "series", s1.id, "wikipedia", "wikipedia:4053941") is True
    # s2's ja-edition pageid numerically collides with s1's bare legacy row —
    # the existing mapping is kept (dedup candidate), nothing inserted.
    assert await add_external_id(db_session, "series", s2.id, "wikipedia", "wikipedia:ja:4053941") is False
    assert await list_external_ids(db_session, "series", s2.id) == []


async def test_merge_bags_dedups_wikipedia_by_pageid(db_session):
    survivor = _series(title_cn="留存")
    dup = _series(title_cn="重复", external_source="wikipedia", external_id="wikipedia:7301786")
    db_session.add_all([survivor, dup])
    await db_session.flush()
    await add_external_id(db_session, "series", survivor.id, "wikipedia", "wikipedia:zh:7301786")
    await add_external_id(db_session, "series", dup.id, "wikipedia", "wikipedia:en:65944845")

    await merge_external_id_bags(db_session, survivor, [dup])
    pairs = await _bag_pairs(db_session, "series", survivor.id)
    # The duplicate's bare primary is the SAME pageid as the survivor's
    # qualified row — dropped, not duplicated.
    assert pairs == {
        ("wikipedia", "wikipedia:zh:7301786"),
        ("wikipedia", "wikipedia:en:65944845"),
    }
