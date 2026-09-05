"""Tests for the per-season work model P3: granularity-aware metadata upsert,
two-level title fallback, lazy season-work creation, and the reworked
season/episode reconciliation (resolve_missing_work / collection-member
absolute locate).
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy import select

from app.models.episode import Episode
from app.models.series import TVSeries
from app.models.work_collection import WorkCollection
from app.models.work_external_id import WorkExternalId
from app.services import metadata_service as ms
from app.services.external_ids import add_external_id
from app.services.metadata_episode_reconcile import (
    is_unsplit_legacy_series,
    park_resource_on_collection,
    resolve_missing_work,
    seasons_map_for_work,
    work_verified_season,
)
from app.services.metadata_source_registry import (
    canonicalize_external_id,
    granularity_of,
    make_season_identity,
    split_season_identity,
)


def _uuid() -> str:
    return str(uuid.uuid4())


def _no_poster():
    return patch(
        "app.services.metadata_service.download_and_cache_poster",
        new_callable=AsyncMock, return_value=None,
    )


async def _series_rows(db_session) -> list[TVSeries]:
    return list((await db_session.execute(select(TVSeries))).scalars().all())


async def _collection_rows(db_session) -> list[WorkCollection]:
    return list((await db_session.execute(select(WorkCollection))).scalars().all())


def _bag_rows(work_type, work_id):
    return select(WorkExternalId).where(
        WorkExternalId.work_type == work_type,
        WorkExternalId.work_id == work_id,
    )


# ---------------------------------------------------------------------------
# Registry granularity + synthetic identities
# ---------------------------------------------------------------------------


def test_granularity_of_registry_sources():
    assert granularity_of("wikipedia") == "series"
    assert granularity_of("tmdb") == "series"
    assert granularity_of("imdb") == "series"
    assert granularity_of("bangumi") == "season"
    assert granularity_of("mal") == "season"
    assert granularity_of("anilist") == "season"
    assert granularity_of("douban") == "season"
    # Movie forms of tmdb/douban are movie-granular.
    assert granularity_of("tmdb", "movie") == "movie"
    assert granularity_of("douban", "movie") == "movie"
    assert granularity_of("wikipedia", "movie") == "series"
    assert granularity_of("exa") is None
    assert granularity_of(None) is None


def test_canonicalize_passes_through_synthetic_season_identity():
    assert canonicalize_external_id("tmdb:82684#s3", "tmdb") == "tmdb:82684#s3"
    assert (
        canonicalize_external_id("wikipedia:zh:8498329#s3", "wikipedia")
        == "wikipedia:zh:8498329#s3"
    )
    # TMDB digit collapse must not eat the suffix.
    assert canonicalize_external_id("TMDB 82684#s3", "exa") == "tmdb:82684#s3"
    # The legacy "/ season N" clutter rule still applies to plain ids.
    assert canonicalize_external_id("TMDB TV 82684 / season 4", "exa") == "tmdb:82684"


def test_make_and_split_season_identity():
    assert make_season_identity("tmdb:82684", 3) == "tmdb:82684#s3"
    assert split_season_identity("tmdb:82684#s3") == ("tmdb:82684", 3)
    assert split_season_identity("wikipedia:zh:8498329#s10") == ("wikipedia:zh:8498329", 10)
    assert split_season_identity("tmdb:82684") is None
    assert split_season_identity(None) is None


# ---------------------------------------------------------------------------
# Upsert: per-season (bangumi) identity
# ---------------------------------------------------------------------------


async def test_bangumi_id_hits_existing_season_work(db_session):
    """A season-granularity id bagged on a season work converges directly."""
    work = TVSeries(
        id=_uuid(), title_cn="无职转生", content_type="tv", season_number=3,
        external_id="bangumi:501963", external_source="bangumi",
    )
    db_session.add(work)
    await db_session.flush()
    # Bag the id on the work (post-P3 bagging style).
    await add_external_id(db_session, "series", work.id, "bangumi", "501963")

    with _no_poster():
        result = await ms.create_or_update_series_from_external(db_session, {
            "content_type": "tv",
            "title_cn": "无职转生 第三季",
            "external_id": "bangumi:501963",
            "external_source": "bangumi",
            "single_season_entry": True,
        })
    assert result is not None
    assert result.id == work.id
    # No collection/work was created for a direct bag hit on a legacy row.
    assert await _collection_rows(db_session) == []
    assert len(await _series_rows(db_session)) == 1


async def test_bangumi_match_creates_collection_and_season_work(db_session):
    with _no_poster():
        work = await ms.create_or_update_series_from_external(
            db_session,
            {
                "content_type": "tv",
                "title_cn": "独自一人的异世界攻略",
                "external_id": "bangumi:12345",
                "external_source": "bangumi",
                "single_season_entry": True,
                "number_of_episodes": 12,
                "episode_list": [
                    {"season": 1, "episode": 1, "title": "第1话"},
                    {"season": 1, "episode": 2, "title": "第2话"},
                ],
            },
        )
    assert work is not None
    assert work.season_number == 1
    assert work.collection_id is not None
    # bangumi is season-granular: the id bags on the WORK, not the collection.
    work_bag = list((await db_session.execute(
        _bag_rows("series", work.id)
    )).scalars().all())
    assert {r.external_id for r in work_bag} == {"bangumi:12345"}
    coll_bag = list((await db_session.execute(
        _bag_rows("collection", work.collection_id)
    )).scalars().all())
    assert coll_bag == []
    # Season-granularity episode_list is re-tagged to the work's season.
    episodes = list((await db_session.execute(
        select(Episode).where(Episode.series_id == work.id)
    )).scalars().all())
    assert {(e.season, e.episode) for e in episodes} == {(1, 1), (1, 2)}


# ---------------------------------------------------------------------------
# Upsert: series-level identity → collection → lazy season work
# ---------------------------------------------------------------------------


def _tmdb_multi_season_entity(**overrides):
    entity = {
        "content_type": "tv",
        "title_cn": "关于我转生变成史莱姆这档事",
        "title_en": "That Time I Got Reincarnated as a Slime",
        "external_id": "82684",
        "external_source": "tmdb",
        "seasons": [
            {"season_number": 1, "episode_count": 24},
            {"season_number": 2, "episode_count": 24},
            {"season_number": 3, "episode_count": 12},
        ],
        "number_of_episodes": 60,
        "episode_list": [
            {"season": 3, "episode": 1, "title": "S3E1", "air_date": "2026-04-03"},
            {"season": 3, "episode": 2, "title": "S3E2", "air_date": "2026-04-10"},
            {"season": 2, "episode": 24, "title": "S2E24", "air_date": "2024-09-27"},
        ],
    }
    entity.update(overrides)
    return entity


async def test_series_level_id_lazily_creates_season_work(db_session):
    """tmdb identity + season hint 3 → collection + lazily-created S3 work."""
    with _no_poster():
        work = await ms.create_or_update_series_from_external(
            db_session, _tmdb_multi_season_entity(), season_hint=3,
        )
    assert work is not None
    assert work.season_number == 3
    assert work.collection_id is not None
    # Per-season data comes from the parent entry's season-3 slice only.
    assert work.number_of_episodes == 12
    assert str(work.start_date) == "2026-04-03"
    # The synthetic per-season identity is the primary + bagged; the
    # series-level tmdb id is bagged on the COLLECTION.
    assert work.external_id == "tmdb:82684#s3"
    work_bag = {r.external_id for r in (await db_session.execute(
        _bag_rows("series", work.id)
    )).scalars().all()}
    assert "tmdb:82684#s3" in work_bag
    coll_bag = {r.external_id for r in (await db_session.execute(
        _bag_rows("collection", work.collection_id)
    )).scalars().all()}
    assert "tmdb:82684" in coll_bag
    # Only the season-3 episode subset landed; the inert columns stay empty.
    episodes = list((await db_session.execute(
        select(Episode).where(Episode.series_id == work.id)
    )).scalars().all())
    assert {(e.season, e.episode) for e in episodes} == {(3, 1), (3, 2)}
    assert work.seasons is None
    assert work.number_of_seasons is None


async def test_synthetic_identity_repeat_match_is_idempotent(db_session):
    """Repeat matches of the same series+season never create new rows."""
    with _no_poster():
        w1 = await ms.create_or_update_series_from_external(
            db_session, _tmdb_multi_season_entity(), season_hint=3,
        )
        # Again via the series-level id (collection bag → member select).
        w2 = await ms.create_or_update_series_from_external(
            db_session, _tmdb_multi_season_entity(), season_hint=3,
        )
        # Again via the synthetic identity directly.
        w3 = await ms.create_or_update_series_from_external(
            db_session,
            _tmdb_multi_season_entity(external_id="tmdb:82684#s3"),
        )
    assert w1.id == w2.id == w3.id
    assert len(await _series_rows(db_session)) == 1
    assert len(await _collection_rows(db_session)) == 1


async def test_season_air_dates_from_infobox_entries(db_session):
    """Wikipedia-style entity: seasons entries carry air_date/end_date from
    the infobox broadcast fields - the lazily-created season work takes its
    OWN premiere/finale even with no episode_list at all."""
    entity = _tmdb_multi_season_entity(
        external_id="wikipedia:zh:5659657",
        external_source="wikipedia",
        seasons=[
            {"season_number": 1, "episode_count": 14, "air_date": "2019-10-02", "end_date": "2019-12-25"},
            {"season_number": 2, "episode_count": 12, "air_date": "2020-04-05", "end_date": "2020-06-21"},
            {"season_number": 3, "episode_count": 10, "air_date": "2022-04-12"},
        ],
        episode_list=[],
        start_date="2019-10-02",
    )
    with _no_poster():
        work = await ms.create_or_update_series_from_external(
            db_session, entity, season_hint=2,
        )
    assert work is not None
    assert work.season_number == 2
    assert str(work.start_date) == "2020-04-05"
    assert str(work.end_date) == "2020-06-21"
    assert work.external_id == "wikipedia:zh:5659657#s2"


async def test_series_level_id_single_member_collection_defaults_to_it(db_session):
    """No season hint + a collection with exactly one member → that member."""
    with _no_poster():
        w1 = await ms.create_or_update_series_from_external(
            db_session, _tmdb_multi_season_entity(), season_hint=3,
        )
        w2 = await ms.create_or_update_series_from_external(
            db_session, _tmdb_multi_season_entity(), season_hint=None,
        )
    assert w2 is not None
    assert w2.id == w1.id


async def test_indeterminate_season_parks_on_collection(db_session):
    """Multi-member collection + no season evidence → None (挂合集待确认)."""
    with _no_poster():
        s1 = await ms.create_or_update_series_from_external(
            db_session, _tmdb_multi_season_entity(), season_hint=1,
        )
        await ms.create_or_update_series_from_external(
            db_session, _tmdb_multi_season_entity(), season_hint=3,
        )
        result = await ms.create_or_update_series_from_external(
            db_session, _tmdb_multi_season_entity(), season_hint=None,
        )
    assert result is None
    # No third work materialized.
    assert len(await _series_rows(db_session)) == 2
    # The collection is re-resolvable for the caller's parking step.
    collection = await ms.find_collection_for_entity(
        db_session, _tmdb_multi_season_entity()
    )
    assert collection is not None
    assert collection.id == s1.collection_id


# ---------------------------------------------------------------------------
# Two-level title fallback
# ---------------------------------------------------------------------------


async def test_two_level_title_fallback_creates_season_work_in_collection(db_session):
    """无职转生自愈回放: wikipedia 建合集+S1；bangumi 逐季条目经标题兜底进同一合集建 S3。"""
    with _no_poster():
        w1 = await ms.create_or_update_series_from_external(db_session, {
            "content_type": "tv",
            "title_cn": "无职转生",
            "external_id": "wikipedia:zh:8498329",
            "external_source": "wikipedia",
            "seasons": [{"season_number": 1, "episode_count": 23}],
        })
        assert w1.season_number == 1
        bangumi_work = await ms.create_or_update_series_from_external(db_session, {
            "content_type": "tv",
            "title_cn": "无职转生 第三季",
            "external_id": "bangumi:501963",
            "external_source": "bangumi",
            "single_season_entry": True,
            "number_of_episodes": 13,
        })
    assert bangumi_work is not None
    assert bangumi_work.season_number == 3
    assert bangumi_work.collection_id == w1.collection_id
    assert bangumi_work.number_of_episodes == 13
    # The bangumi id bags on the season work.
    bag = {r.external_id for r in (await db_session.execute(
        _bag_rows("series", bangumi_work.id)
    )).scalars().all()}
    assert "bangumi:501963" in bag


async def test_third_season_title_no_longer_collides_with_s1_work(db_session):
    """The pre-split bug: 「第三季」stripped to the base title matched the S1
    work. The season-aware fallback must create/select the S3 work instead."""
    with _no_poster():
        s1 = await ms.create_or_update_series_from_external(db_session, {
            "content_type": "tv",
            "title_cn": "某剧",
            "external_id": "245842",
            "external_source": "tmdb",
        })
        s3 = await ms.create_or_update_series_from_external(db_session, {
            "content_type": "tv",
            "title_cn": "某剧 第三季",
            "external_id": "245842",
            "external_source": "tmdb",
        })
    assert s1.season_number == 1
    assert s3.id != s1.id
    assert s3.season_number == 3
    assert s3.collection_id == s1.collection_id
    # Base title is stored; the season-qualified form is an alias.
    assert s3.title_cn == "某剧"
    assert "某剧 第三季" in (s3.aliases or [])


async def test_title_fallback_unknown_season_multi_member_returns_none(db_session):
    """Identity-less entity, collection matched by title, ≥2 members, no
    season evidence → indeterminate (None), nothing created."""
    with _no_poster():
        s1 = await ms.create_or_update_series_from_external(db_session, {
            "content_type": "tv", "title_cn": "某剧",
            "external_id": "245842", "external_source": "tmdb",
        })
        await ms.create_or_update_series_from_external(db_session, {
            "content_type": "tv", "title_cn": "某剧 第二季",
            "external_id": "245842", "external_source": "tmdb",
        })
        result = await ms.create_or_update_series_from_external(db_session, {
            "content_type": "tv", "title_cn": "某剧",
            "external_id": None, "external_source": "exa_web",
        })
    assert result is None
    assert len(await _series_rows(db_session)) == 2
    collection = await ms.find_collection_for_entity(db_session, {
        "content_type": "tv", "title_cn": "某剧", "external_source": "exa_web",
    })
    assert collection is not None
    assert collection.id == s1.collection_id


# ---------------------------------------------------------------------------
# resolve_missing_work / work season evidence
# ---------------------------------------------------------------------------


def _resource_ns(**overrides):
    base = dict(
        season=None, episode=3, absolute_episode=None, is_batch=False,
        batch_scope=None, episode_confidence=None, title_cn=None, title_en=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_work_verified_season_rules():
    # Post-split per-season rows: season_number != 1 is trusted directly.
    assert work_verified_season(SimpleNamespace(
        season_number=3, seasons=None, number_of_seasons=None,
        external_source="tmdb", external_id="tmdb:1#s3",
    )) == 3
    # season_number == 1 needs single-season evidence…
    assert work_verified_season(SimpleNamespace(
        season_number=1, seasons=None, number_of_seasons=None,
        external_source="wikipedia", external_id="wikipedia:zh:1",
    )) is None
    # … a Bangumi entry identity is that evidence…
    assert work_verified_season(SimpleNamespace(
        season_number=1, seasons=None, number_of_seasons=None,
        external_source="bangumi", external_id="bangumi:1",
    )) == 1
    # … and an unsplit legacy multi-season row never yields a season.
    legacy = SimpleNamespace(
        season_number=1, number_of_seasons=3,
        seasons=[{"season_number": n, "episode_count": 12} for n in (1, 2, 3)],
        external_source="tmdb", external_id="tmdb:9",
    )
    assert is_unsplit_legacy_series(legacy) is True
    assert work_verified_season(legacy) is None
    assert work_verified_season(None) is None


def test_resolve_missing_work_uses_work_identity_season():
    work = SimpleNamespace(
        season_number=3, seasons=None, number_of_seasons=None,
        external_source="tmdb", external_id="tmdb:1#s3",
    )
    r = _resource_ns()
    assert resolve_missing_work(r, None, work=work) == "season-defaulted"
    assert r.season == 3
    assert r.episode_confidence is None  # defaulting is not "raw" evidence


def test_resolve_missing_work_legacy_unknown_stays_ambiguous():
    legacy = SimpleNamespace(
        season_number=1, seasons=None, number_of_seasons=None,
        external_source="tmdb", external_id="tmdb:9",
    )
    r = _resource_ns()
    assert resolve_missing_work(r, None, work=legacy) == "marked-ambiguous"
    assert r.season is None
    assert r.episode_confidence == "ambiguous"


def test_resolve_missing_work_batch_semantics_kept():
    work = SimpleNamespace(
        season_number=2, seasons=None, number_of_seasons=None,
        external_source="bangumi", external_id="bangumi:7",
    )
    # Season-scope pack takes the verified season default…
    r = _resource_ns(is_batch=True, batch_scope="season", episode=None)
    assert resolve_missing_work(r, None, work=work) == "season-defaulted"
    assert r.season == 2
    assert r.episode_confidence is None
    # … and multi-season packs are a full no-op (never ambiguous).
    r2 = _resource_ns(is_batch=True, batch_scope="multi_season", episode=None)
    assert resolve_missing_work(r2, None, work=work) is None
    assert r2.season is None
    assert r2.episode_confidence is None


def test_park_resource_on_collection():
    coll = SimpleNamespace(id="coll-1")
    r = _resource_ns(series_id="w1", movie_id=None, collection_id=None)
    park_resource_on_collection(r, coll)
    assert r.collection_id == "coll-1"
    assert r.series_id is None
    assert r.episode_confidence == "ambiguous"
    # Batches park without the ambiguous flag; manual rows are untouched.
    r2 = _resource_ns(is_batch=True, series_id="w2")
    park_resource_on_collection(r2, coll)
    assert r2.collection_id == "coll-1"
    assert r2.episode_confidence is None
    r3 = _resource_ns(episode_confidence="manual", series_id="w3")
    park_resource_on_collection(r3, coll)
    assert r3.episode_confidence == "manual"


def test_seasons_map_for_work():
    legacy = SimpleNamespace(
        seasons=[{"season_number": 1, "episode_count": 12}],
        number_of_episodes=None, season_number=1,
    )
    assert seasons_map_for_work(legacy) == {1: 12}
    per_season = SimpleNamespace(seasons=None, number_of_episodes=13, season_number=3)
    assert seasons_map_for_work(per_season) == {3: 13}
    empty = SimpleNamespace(seasons=None, number_of_episodes=None, season_number=1)
    assert seasons_map_for_work(empty) == {}


# ---------------------------------------------------------------------------
# Collection-member absolute-episode locate + linked-resource reconcile
# ---------------------------------------------------------------------------


async def _make_collection_with_members(db_session):
    coll = WorkCollection(id=_uuid(), title_cn="某剧", external_source="series_group")
    db_session.add(coll)
    s1 = TVSeries(id=_uuid(), title_cn="某剧", content_type="tv",
                  season_number=1, number_of_episodes=12, collection_id=coll.id)
    s2 = TVSeries(id=_uuid(), title_cn="某剧", content_type="tv",
                  season_number=2, number_of_episodes=12, collection_id=coll.id)
    db_session.add_all([s1, s2])
    await db_session.flush()
    return coll, s1, s2


async def test_locate_absolute_episode_in_collection(db_session):
    coll, s1, s2 = await _make_collection_with_members(db_session)
    member, episode = await ms.locate_absolute_episode_in_collection(db_session, coll.id, 5)
    assert (member.id, episode) == (s1.id, 5)
    member, episode = await ms.locate_absolute_episode_in_collection(db_session, coll.id, 15)
    assert (member.id, episode) == (s2.id, 3)
    # Last season gets the tolerance headroom (12 + 2); the inferred number
    # is preserved, not clamped onto a stale metadata count.
    member, episode = await ms.locate_absolute_episode_in_collection(db_session, coll.id, 26)
    assert (member.id, episode) == (s2.id, 14)
    # Beyond the total + tolerance → None.
    assert await ms.locate_absolute_episode_in_collection(db_session, coll.id, 30) is None
    # Unknown member count aborts the walk.
    s2.number_of_episodes = None
    await db_session.flush()
    assert await ms.locate_absolute_episode_in_collection(db_session, coll.id, 15) is None


async def test_reconcile_linked_resource_repoints_along_collection(db_session):
    """A season-less resource with an absolute number is located along the
    collection members and re-pointed at the located season work."""
    coll, s1, s2 = await _make_collection_with_members(db_session)
    resource = SimpleNamespace(
        series_id=s1.id, season=None, episode=None, absolute_episode=15,
        is_batch=False, batch_scope=None, episode_confidence=None,
        title_cn=None, title_en=None, channel_id="ch", id=_uuid(),
        subtitle_group=None, subtitle_groups=None,
    )
    await ms.reconcile_linked_series_resource(db_session, resource, series=s1)
    assert resource.series_id == s2.id
    assert (resource.season, resource.episode) == (2, 3)
    assert resource.episode_confidence == "reconciled"


async def test_reconcile_linked_resource_defaults_season_from_work_identity(db_session):
    coll = WorkCollection(id=_uuid(), title_cn="某剧", external_source="series_group")
    db_session.add(coll)
    s3 = TVSeries(id=_uuid(), title_cn="某剧", content_type="tv",
                  season_number=3, number_of_episodes=13, collection_id=coll.id,
                  external_id="tmdb:82684#s3", external_source="tmdb")
    db_session.add(s3)
    await db_session.flush()
    resource = SimpleNamespace(
        series_id=s3.id, season=None, episode=5, absolute_episode=None,
        is_batch=False, batch_scope=None, episode_confidence=None,
        title_cn=None, title_en=None, channel_id="ch", id=_uuid(),
        subtitle_group=None, subtitle_groups=None,
    )
    await ms.reconcile_linked_series_resource(db_session, resource, series=s3)
    assert resource.season == 3
    # apply_episode_reconcile's no-basis marking ran first (same as every
    # pre-existing link path); the season itself came from the work identity.
    assert resource.episode_confidence == "raw"


# ---------------------------------------------------------------------------
# Identity bag: legacy rows still absorb their ids (pre-migration compat)
# ---------------------------------------------------------------------------


async def test_legacy_series_row_keeps_absorbing_series_level_id(db_session):
    """A pre-split row owning the tmdb primary converges as before (no new
    collection) until the season-split migration re-homes it."""
    legacy = TVSeries(
        id=_uuid(), title_cn="Legacy Show", content_type="tv",
        external_id="tmdb:555", external_source="tmdb",
        seasons=[{"season_number": 1, "episode_count": 12},
                 {"season_number": 2, "episode_count": 12}],
        number_of_seasons=2,
    )
    db_session.add(legacy)
    await db_session.flush()
    # Bag the id on the work (pre-P3 bagging style).
    await add_external_id(db_session, "series", legacy.id, "tmdb", "555")

    with _no_poster():
        result = await ms.create_or_update_series_from_external(
            db_session,
            {
                "content_type": "tv",
                "title_en": "Legacy Show",
                "external_id": "82684-ish-unused",
                "external_source": "tmdb",
                "seasons": [{"season_number": 1, "episode_count": 12},
                            {"season_number": 2, "episode_count": 12}],
            },
            season_hint=None,
        )
    # Title fallback matched the legacy row (season unknown, all-S1-season set
    # does not apply here: the row's season_number defaults to 1 and it is the
    # single candidate).
    assert result is not None
    assert result.id == legacy.id
    assert await _collection_rows(db_session) == []
