"""Unit tests for app.services.bangumi_relations.

Pure helpers are exercised directly with crafted dicts/objects; the DB-backed
helpers and the ``expand_bangumi_series_graph`` orchestrator run against the
shared ``db_session`` fixture with all Bangumi HTTP mocked at the module
boundary (``br.get_subject_relations`` / ``br.get_subject`` for the graph walk
and ``mb.get_subject`` / ``mb.get_subject_episodes`` for entity building).
"""

from __future__ import annotations

import uuid
from datetime import date
from types import SimpleNamespace

from sqlalchemy import select

import app.services.bangumi_relations as br
import app.services.metadata_bangumi as mb
from app.models.channel import Channel
from app.models.episode import Episode
from app.models.file_resource import FileResource
from app.models.movie import Movie
from app.models.resource_work_link import ResourceWorkLink
from app.models.series import TVSeries
from app.models.work_collection import WorkCollection
from app.models.work_external_id import WorkExternalId


def _uuid() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


async def _make_channel(db_session) -> Channel:
    ch = Channel(
        id=_uuid(),
        name="Bangumi Relations Channel",
        type="rss_feed",
        url="https://example.com/rss",
        fetch_interval=1800,
        status="active",
        field_mapping={"list_locator": {"source": "entries"}},
        metadata_agent_enabled=False,
    )
    db_session.add(ch)
    await db_session.flush()
    return ch


async def _make_resource(db_session, **over) -> FileResource:
    if "channel_id" not in over:
        ch = await _make_channel(db_session)
        over["channel_id"] = ch.id
    base = dict(
        id=_uuid(),
        guid=_uuid(),
        title_raw="头文字D.全六季.Initial D",
        torrent_url="https://x/pack.torrent",
    )
    base.update(over)
    resource = FileResource(**base)
    db_session.add(resource)
    await db_session.flush()
    return resource


def _collection(title: str = "头文字D", source: str = "series_group", **over) -> WorkCollection:
    base = dict(
        id=_uuid(),
        title_cn=title,
        external_id=None,
        external_source=source,
    )
    base.update(over)
    return WorkCollection(**base)


def _series(
    title: str,
    *,
    season: int = 1,
    source: str = "bangumi",
    subj: int | None = None,
    collection_id: str | None = None,
    **over,
) -> TVSeries:
    base = dict(
        id=_uuid(),
        title_cn=title,
        original_title=title,
        external_source=source,
        external_id=f"bangumi:{subj}" if subj is not None else None,
        content_type="tv",
        season_number=season,
        collection_id=collection_id,
    )
    base.update(over)
    return TVSeries(**base)


async def _links(db_session, resource_id) -> list[ResourceWorkLink]:
    return (
        await db_session.execute(
            select(ResourceWorkLink).where(ResourceWorkLink.resource_id == resource_id)
        )
    ).scalars().all()


# ---------------------------------------------------------------------------
# Pure helpers — classify_work_shape
# ---------------------------------------------------------------------------


class TestClassifyWorkShape:
    def test_relationless_shapes(self):
        assert br.classify_work_shape(None, "剧场版") == "movie"
        assert br.classify_work_shape(None, "电影") == "movie"
        assert br.classify_work_shape(None, "OVA") == "specials"
        assert br.classify_work_shape(None, "OAD") == "specials"
        assert br.classify_work_shape(None, "TV") == "season"
        assert br.classify_work_shape(None, "") == "season"

    def test_movie_relation(self):
        assert br.classify_work_shape("剧场版", "TV") == "movie"
        assert br.classify_work_shape("剧场版", "剧场版") == "movie"

    def test_specials_relation(self):
        assert br.classify_work_shape("番外篇", "TV") == "specials"
        assert br.classify_work_shape("番外篇", "OVA") == "specials"
        assert br.classify_work_shape("番外篇", "剧场版") == "movie"

    def test_adaptation_relation(self):
        assert br.classify_work_shape("不同演绎", "剧场版") == "movie"
        assert br.classify_work_shape("不同演绎", "TV") is None

    def test_sequel_relation(self):
        assert br.classify_work_shape("续集", "TV") == "season"
        assert br.classify_work_shape("前传", "TV") == "season"
        assert br.classify_work_shape("主线故事", "TV") == "season"
        assert br.classify_work_shape("续集", "剧场版") == "movie"

    def test_unknown_relation(self):
        assert br.classify_work_shape("相同世界观", "TV") is None
        assert br.classify_work_shape("片头曲", "TV") is None


# ---------------------------------------------------------------------------
# Pure helpers — _base_names / _same_ip / _bangumi_id_of / _node_date
# ---------------------------------------------------------------------------


class TestBaseNamesAndSameIp:
    def test_base_names_skips_empty_and_strips(self):
        names = br._base_names(["头文字D 第二季", None, "", "頭文字D"])
        assert names
        assert br.normalize_title("头文字D") in names

    def test_same_ip_equality_containment_and_miss(self):
        # equality after strip
        assert br._same_ip(br._base_names(["頭文字d"]), ("頭文字D",)) is True
        # containment (node base contained in a seed base)
        assert br._same_ip(br._base_names(["頭文字d final stage"]), ("頭文字d final",)) is True
        # two-char floor prevents single-letter containment
        assert br._same_ip(br._base_names(["abc"]), ("a",)) is False
        # no overlap at all
        assert br._same_ip(br._base_names(["頭文字d"]), ("MFゴースト",)) is False

    def test_bangumi_id_of(self):
        assert br._bangumi_id_of(None) is None
        assert br._bangumi_id_of("") is None
        assert br._bangumi_id_of("wikipedia:123") is None
        assert br._bangumi_id_of("bangumi:abc") is None
        assert br._bangumi_id_of("bangumi:42") == 42

    def test_node_date(self):
        assert br._node_date({"detail": {"date": "2000-01-01"}, "summary": {}}) == "2000-01-01"
        assert br._node_date({"detail": {}, "summary": {"date": "1999-05-05"}}) == "1999-05-05"
        assert br._node_date({"detail": {"date": "abcd"}, "summary": {}}) is None
        assert br._node_date({"detail": {}, "summary": {}}) is None
        assert br._node_date({"detail": {"date": ""}, "summary": {}}) is None


# ---------------------------------------------------------------------------
# Pure helpers — _assign_seasons
# ---------------------------------------------------------------------------


class TestAssignSeasons:
    def test_marker_first_then_marked_seed(self):
        seeds = [{"sid": 1, "work_type": "series", "season": 1, "date": "1998-04-18"}]
        nodes = [
            {"sid": 2, "summary": {"name": "頭文字D Second Stage", "name_cn": None},
             "detail": {"date": "1999-10-15"}},
        ]
        assert br._assign_seasons(nodes, seeds) == {1: 1, 2: 2}

    def test_all_marked_returns_early(self):
        seeds = [{"sid": 1, "work_type": "series", "season": 1, "date": "1998-04-18"}]
        nodes = [
            {"sid": 5, "summary": {"name": "頭文字D Fifth Stage", "name_cn": None},
             "detail": {"date": "2012-11-04"}},
        ]
        assert br._assign_seasons(nodes, seeds) == {1: 1, 5: 5}

    def test_unmarked_without_dates_is_skipped(self):
        seeds = [{"sid": 1, "work_type": "series", "season": 1, "date": "1998-04-18"}]
        nodes = [
            {"sid": 9, "summary": {"name": "頭文字D 新作", "name_cn": None},
             "detail": {"date": None}},
        ]
        assert br._assign_seasons(nodes, seeds) == {1: 1}

    def test_no_dates_anywhere_logs_and_skips(self):
        seeds = [{"sid": 1, "work_type": "series", "season": None, "date": None}]
        nodes = [
            {"sid": 9, "summary": {"name": "頭文字D 新作", "name_cn": None}, "detail": {}},
        ]
        assert br._assign_seasons(nodes, seeds) == {}

    def test_duplicate_air_dates_skip_all_inferable(self):
        seeds = [{"sid": 1, "work_type": "series", "season": 1, "date": "1998-04-18"}]
        nodes = [
            {"sid": 2, "summary": {"name": "頭文字D Second Stage", "name_cn": None},
             "detail": {"date": "1999-10-15"}},
            {"sid": 3, "summary": {"name": "頭文字D 特別編", "name_cn": None},
             "detail": {"date": "2001-01-01"}},
            {"sid": 4, "summary": {"name": "頭文字D 番外", "name_cn": None},
             "detail": {"date": "2001-01-01"}},
        ]
        assert br._assign_seasons(nodes, seeds) == {1: 1, 2: 2}

    def test_unique_unmarked_after_threshold_is_inferred(self):
        seeds = [{"sid": 1, "work_type": "series", "season": 1, "date": "1998-04-18"}]
        nodes = [
            {"sid": 2, "summary": {"name": "頭文字D Second Stage", "name_cn": None},
             "detail": {"date": "1999-10-15"}},
            {"sid": 8, "summary": {"name": "頭文字D Final Stage", "name_cn": None},
             "detail": {"date": "2014-05-16"}},
        ]
        assert br._assign_seasons(nodes, seeds) == {1: 1, 2: 2, 8: 3}

    def test_seed_without_season_not_marked(self):
        seeds = [{"sid": 1, "work_type": "movie", "season": 4, "date": "2000-01-01"}]
        nodes = [
            {"sid": 2, "summary": {"name": "頭文字D Second Stage", "name_cn": None},
             "detail": {"date": "1999-10-15"}},
        ]
        # Only series seeds contribute to ``marked``.
        assert br._assign_seasons(nodes, seeds) == {2: 2}


# ---------------------------------------------------------------------------
# Pure-ish helpers — _season_is_default_guess
# ---------------------------------------------------------------------------


class TestSeasonIsDefaultGuess:
    def test_marked_title_is_not_a_guess(self):
        work = SimpleNamespace(title_cn="头文字D 第二季", title_en=None, original_title=None)
        assert br._season_is_default_guess(work) is False

    def test_qualified_unmarked_title_is_a_guess(self):
        work = SimpleNamespace(title_cn=None, title_en=None, original_title="頭文字D Final Stage")
        assert br._season_is_default_guess(work) is True

    def test_plain_base_title_is_not_a_guess(self):
        work = SimpleNamespace(title_cn="头文字D", title_en=None, original_title="頭文字D")
        assert br._season_is_default_guess(work) is False


# ---------------------------------------------------------------------------
# DB helpers — _work_subject_id / _seed_subjects
# ---------------------------------------------------------------------------


async def test_work_subject_id_primary_column(db_session):
    work = _series("x", subj=7)
    db_session.add(work)
    await db_session.flush()
    assert await br._work_subject_id(db_session, "series", work) == 7


async def test_work_subject_id_from_bag(db_session):
    work = _series("x", source="manual")
    db_session.add(work)
    await db_session.flush()
    db_session.add(WorkExternalId(
        work_type="series", work_id=work.id, source="bangumi", external_id="bangumi:11",
    ))
    await db_session.flush()
    assert await br._work_subject_id(db_session, "series", work) == 11


async def test_work_subject_id_none_when_unknown(db_session):
    work = _series("x", source="manual")
    db_session.add(work)
    await db_session.flush()
    assert await br._work_subject_id(db_session, "series", work) is None


async def test_seed_subjects_from_all_sources(db_session):
    collection = _collection("头文字D")
    s1 = _series("头文字D", season=1, subj=1, collection_id=collection.id,
                 start_date=date(1998, 4, 18))
    m1 = Movie(
        id=_uuid(), title_cn="头文字D 剧场版", external_source="bangumi",
        external_id="bangumi:3", content_type="movie", release_date=date(2001, 1, 13),
    )
    s3 = _series("头文字D Third Stage", source="manual", collection_id=collection.id)
    s4 = _series("头文字D 无身份", source="manual", collection_id=collection.id)
    m2 = Movie(
        id=_uuid(), title_cn="头文字D Legend", external_source="bangumi",
        external_id="bangumi:7", content_type="movie", collection_id=collection.id,
    )
    db_session.add_all([collection, s1, m1, s3, s4, m2])
    await db_session.flush()
    db_session.add_all([
        WorkExternalId(work_type="series", work_id=s3.id, source="bangumi", external_id="bangumi:5"),
    ])
    resource = await _make_resource(
        db_session, is_batch=True, batch_scope="franchise",
        series_id=s1.id, movie_id=m1.id, collection_id=collection.id,
    )
    db_session.add_all([
        ResourceWorkLink(resource_id=resource.id, series_id=s1.id, source="auto"),
        ResourceWorkLink(resource_id=resource.id, series_id=s3.id, source="auto"),
    ])
    await db_session.flush()

    seeds = await br._seed_subjects(db_session, resource)
    by_sid = {s["sid"]: s for s in seeds}
    # Primary column, movie FK, bag-only identity, and collection movie member.
    assert set(by_sid) == {1, 3, 5, 7}
    assert by_sid[1]["work_type"] == "series"
    assert by_sid[1]["date"] == "1998-04-18"
    assert by_sid[3]["work_type"] == "movie"
    assert by_sid[3]["date"] == "2001-01-13"
    # The link to s3 is deduped from the franchise collection scan.
    assert by_sid[5]["work_type"] == "series"
    assert by_sid[7]["work_type"] == "movie"
    # s4 has no Bangumi identity and is skipped.
    assert all(s["work"].id != s4.id for s in seeds)


async def test_seed_subjects_non_franchise_ignores_collection_members(db_session):
    collection = _collection("头文字D")
    s1 = _series("头文字D", season=1, subj=1, collection_id=collection.id)
    s2 = _series("头文字D Second Stage", season=2, subj=2, collection_id=collection.id)
    db_session.add_all([collection, s1, s2])
    await db_session.flush()
    resource = await _make_resource(
        db_session, is_batch=True, batch_scope="multi_season",
        series_id=s1.id, collection_id=collection.id,
    )
    seeds = await br._seed_subjects(db_session, resource)
    assert {s["sid"] for s in seeds} == {1}


# ---------------------------------------------------------------------------
# DB helpers — _ensure_link / _collection_member_by_season / _season_zero_holder
# ---------------------------------------------------------------------------


async def test_ensure_link_additive(db_session):
    resource = await _make_resource(db_session)
    s1 = _series("s1", subj=1)
    m1 = Movie(id=_uuid(), title_cn="m1", external_source="bangumi", external_id="bangumi:2",
               content_type="movie")
    db_session.add_all([s1, m1])
    await db_session.flush()

    assert await br._ensure_link(db_session, resource, "series", s1.id) is True
    # Idempotent: second call sees the existing row.
    assert await br._ensure_link(db_session, resource, "series", s1.id) is False
    assert await br._ensure_link(db_session, resource, "movie", m1.id) is True
    links = await _links(db_session, resource.id)
    assert all(link.source == "auto" for link in links)
    assert {link.series_id or link.movie_id for link in links} == {s1.id, m1.id}


async def test_collection_member_by_season(db_session):
    collection = _collection()
    s2 = _series("s2", season=2, collection_id=collection.id)
    db_session.add_all([collection, s2])
    await db_session.flush()

    assert await br._collection_member_by_season(db_session, collection.id, None) is None
    found = await br._collection_member_by_season(db_session, collection.id, 2)
    assert found is not None and found.id == s2.id
    assert await br._collection_member_by_season(db_session, collection.id, 9) is None


async def test_season_zero_holder(db_session):
    collection = _collection()
    s0 = _series("specials", season=0, collection_id=collection.id)
    db_session.add_all([collection, s0])
    await db_session.flush()
    holder = await br._season_zero_holder(db_session, collection.id)
    assert holder is not None and holder.id == s0.id
    assert await br._season_zero_holder(db_session, _uuid()) is None


# ---------------------------------------------------------------------------
# DB helpers — _attach_and_link
# ---------------------------------------------------------------------------


async def test_attach_same_collection_links(db_session):
    collection = _collection()
    work = _series("s1", season=1, subj=1, collection_id=collection.id)
    db_session.add_all([collection, work])
    await db_session.flush()
    resource = await _make_resource(db_session)
    assert await br._attach_and_link(db_session, resource, "series", work, collection) is True
    assert len(await _links(db_session, resource.id)) == 1


async def test_attach_null_collection_assigns_target(db_session):
    target = _collection("target")
    work = _series("s1", season=1, subj=1, collection_id=None)
    db_session.add_all([target, work])
    await db_session.flush()
    resource = await _make_resource(db_session)
    assert await br._attach_and_link(db_session, resource, "series", work, target) is True
    assert work.collection_id == target.id


async def test_attach_slot_occupied_refuses_without_link_foreign(db_session):
    target = _collection("target")
    other = _collection("other")
    occupant = _series("occupant", season=2, subj=2, collection_id=target.id)
    work = _series("s2", season=2, subj=3, collection_id=other.id)
    db_session.add_all([target, other, occupant, work])
    await db_session.flush()
    resource = await _make_resource(db_session)
    assert await br._attach_and_link(db_session, resource, "series", work, target) is False
    assert await _links(db_session, resource.id) == []
    assert work.collection_id == other.id


async def test_attach_slot_occupied_link_foreign(db_session):
    target = _collection("target")
    other = _collection("other", source="manual")
    occupant = _series("occupant", season=2, subj=2, collection_id=target.id)
    work = _series("s2", season=2, subj=3, collection_id=other.id)
    db_session.add_all([target, other, occupant, work])
    await db_session.flush()
    resource = await _make_resource(db_session)
    assert await br._attach_and_link(
        db_session, resource, "series", work, target, link_foreign=True
    ) is True
    assert len(await _links(db_session, resource.id)) == 1


async def test_attach_absorbs_single_member_shell(db_session):
    target = _collection("头文字D")
    shell = _collection("头文字D", source="series_group")
    work = _series("头文字D", season=1, subj=1, collection_id=shell.id)
    db_session.add_all([target, shell, work])
    await db_session.flush()
    resource = await _make_resource(db_session)
    assert await br._attach_and_link(db_session, resource, "series", work, target) is True
    assert work.collection_id == target.id
    assert await db_session.get(WorkCollection, shell.id) is None


async def test_attach_absorbs_same_name_multi_member_collection(db_session):
    target = _collection("头文字D Pack")
    shell = _collection("头文字D", source="series_group")
    work = _series("头文字D Second Stage", season=2, subj=2, collection_id=shell.id)
    other = _series("头文字D", season=1, subj=1, collection_id=shell.id)
    db_session.add_all([target, shell, work, other])
    await db_session.flush()
    resource = await _make_resource(db_session)
    assert await br._attach_and_link(db_session, resource, "series", work, target) is True
    assert work.collection_id == target.id
    assert await db_session.get(WorkCollection, shell.id) is None


async def test_attach_foreign_collection_refuses_without_link_foreign(db_session):
    target = _collection("target")
    other = _collection("unrelated", source="manual")
    work = _series("x", season=1, subj=1, collection_id=other.id)
    db_session.add_all([target, other, work])
    await db_session.flush()
    resource = await _make_resource(db_session)
    assert await br._attach_and_link(db_session, resource, "series", work, target) is False
    assert await _links(db_session, resource.id) == []
    assert work.collection_id == other.id


async def test_attach_foreign_collection_link_foreign(db_session):
    target = _collection("target")
    other = _collection("unrelated", source="manual")
    work = _series("x", season=1, subj=1, collection_id=other.id)
    db_session.add_all([target, other, work])
    await db_session.flush()
    resource = await _make_resource(db_session)
    assert await br._attach_and_link(
        db_session, resource, "series", work, target, link_foreign=True
    ) is True
    assert work.collection_id == other.id
    assert len(await _links(db_session, resource.id)) == 1


async def test_attach_movie_foreign_collection_link_foreign(db_session):
    target = _collection("target")
    other = _collection("unrelated", source="manual")
    movie = Movie(id=_uuid(), title_cn="m", external_source="bangumi",
                  external_id="bangumi:9", content_type="movie", collection_id=other.id)
    db_session.add_all([target, other, movie])
    await db_session.flush()
    resource = await _make_resource(db_session)
    assert await br._attach_and_link(
        db_session, resource, "movie", movie, target, link_foreign=True
    ) is True


# ---------------------------------------------------------------------------
# DB helpers — _correct_work_season
# ---------------------------------------------------------------------------


async def test_correct_work_season_already_equal(db_session):
    work = _series("s", season=2)
    db_session.add(work)
    await db_session.flush()
    assert await br._correct_work_season(db_session, work, 2, {"sid": 2}) is True


async def test_correct_work_season_respects_manual_edit(db_session):
    work = _series("s", season=5, manually_edited_fields=["season_number"])
    db_session.add(work)
    await db_session.flush()
    assert await br._correct_work_season(db_session, work, 6, {"sid": 8}) is False
    assert work.season_number == 5


async def test_correct_work_season_refuses_occupied_slot(db_session):
    collection = _collection()
    work = _series("s", season=2, collection_id=collection.id)
    occupant = _series("occupant", season=6, collection_id=collection.id)
    db_session.add_all([collection, work, occupant])
    await db_session.flush()
    assert await br._correct_work_season(db_session, work, 6, {"sid": 8}) is False
    assert work.season_number == 2


async def test_correct_work_season_applies(db_session):
    collection = _collection()
    work = _series("s", season=2, collection_id=collection.id)
    db_session.add_all([collection, work])
    await db_session.flush()
    assert await br._correct_work_season(db_session, work, 6, {"sid": 8}) is True
    assert work.season_number == 6


async def test_correct_work_season_retags_episodes(db_session):
    """A season correction follows through onto the denormalized Episode rows
    so no stale-season labels linger (corpus residual #3)."""
    collection = _collection()
    work = _series("s", season=2, collection_id=collection.id)
    db_session.add_all([collection, work])
    await db_session.flush()
    db_session.add_all([
        Episode(series_id=work.id, season=2, episode=1),
        Episode(series_id=work.id, season=2, episode=2),
    ])
    await db_session.flush()

    assert await br._correct_work_season(db_session, work, 6, {"sid": 8}) is True
    await db_session.flush()

    rows = (await db_session.execute(
        select(Episode).where(Episode.series_id == work.id)
    )).scalars().all()
    assert {(r.season, r.episode) for r in rows} == {(6, 1), (6, 2)}


async def test_correct_work_season_drops_stale_duplicate_episodes(db_session):
    """An episode number already present at the target season is dropped
    instead of colliding with the (series, season, episode) unique key."""
    collection = _collection()
    work = _series("s", season=2, collection_id=collection.id)
    db_session.add_all([collection, work])
    await db_session.flush()
    db_session.add_all([
        Episode(series_id=work.id, season=2, episode=1),
        Episode(series_id=work.id, season=2, episode=2),
        Episode(series_id=work.id, season=6, episode=1),
    ])
    await db_session.flush()

    assert await br._correct_work_season(db_session, work, 6, {"sid": 8}) is True
    await db_session.flush()

    rows = (await db_session.execute(
        select(Episode).where(Episode.series_id == work.id)
    )).scalars().all()
    assert {(r.season, r.episode) for r in rows} == {(6, 1), (6, 2)}


# ---------------------------------------------------------------------------
# _upsert_season_node
# ---------------------------------------------------------------------------


async def test_upsert_season_node_tv(db_session):
    async def _build(client, subject, *, season, detail=None):
        return {"external_id": "bangumi:99", "_content_type": "tv"}

    created = {}

    async def _create(db, entity, *, season_hint=None):
        work = _series("x", season=season_hint, subj=99)
        db.add(work)
        await db.flush()
        created["work"] = work
        return work

    node = {"sid": 99, "summary": {"id": 99, "name": "x"}, "detail": {"platform": "TV"}}
    work = await br._upsert_season_node(
        db_session, None, node, 3, _create, _build, []
    )
    assert work is created["work"]
    assert work.season_number == 3


async def test_upsert_season_node_movie_form_deferred(db_session):
    async def _build(client, subject, *, season, detail=None):
        return {"external_id": "bangumi:99", "_content_type": "movie"}

    async def _create(db, entity, *, season_hint=None):  # pragma: no cover - not reached
        raise AssertionError("movie-form node must not be upserted as a series")

    node = {"sid": 99, "summary": {"id": 99, "name": "x"}, "detail": {}}
    movie_nodes: list = []
    assert await br._upsert_season_node(
        db_session, None, node, 3, _create, _build, movie_nodes
    ) is None
    assert movie_nodes == [node]


async def test_upsert_season_node_swallows_build_errors(db_session):
    async def _build(client, subject, *, season, detail=None):
        raise ValueError("boom")

    async def _create(db, entity, *, season_hint=None):  # pragma: no cover - not reached
        raise AssertionError

    node = {"sid": 99, "summary": {"id": 99, "name": "x"}, "detail": {}}
    assert await br._upsert_season_node(
        db_session, None, node, 3, _create, _build, []
    ) is None


async def test_upsert_season_node_missing_content_type_is_swallowed(db_session):
    async def _build(client, subject, *, season, detail=None):
        return {"external_id": "bangumi:99"}  # missing ``_content_type`` → KeyError

    async def _create(db, entity, *, season_hint=None):  # pragma: no cover - not reached
        raise AssertionError

    node = {"sid": 99, "summary": {"id": 99, "name": "x"}, "detail": {}}
    assert await br._upsert_season_node(
        db_session, None, node, 3, _create, _build, []
    ) is None


# ---------------------------------------------------------------------------
# _settle_resource
# ---------------------------------------------------------------------------


async def test_settle_resource_merges_seasons_and_ignores_movies(db_session):
    collection = _collection()
    s2 = _series("s2", season=2, subj=2, collection_id=collection.id)
    m1 = Movie(id=_uuid(), title_cn="m", external_source="bangumi", external_id="bangumi:3",
               content_type="movie", collection_id=collection.id)
    db_session.add_all([collection, s2, m1])
    await db_session.flush()
    resource = await _make_resource(
        db_session, is_batch=True, batch_scope="multi_season",
        collection_id=collection.id, batch_seasons=[1],
    )
    await br._settle_resource(
        db_session, resource, {("series", s2.id), ("movie", m1.id)}
    )
    assert resource.batch_seasons == [1, 2]
    assert resource.collection_id == collection.id


async def test_settle_resource_franchise_scope_skips_season_merge(db_session):
    resource = await _make_resource(
        db_session, is_batch=True, batch_scope="franchise", batch_seasons=None,
    )
    s2 = _series("s2", season=2, subj=2)
    db_session.add(s2)
    await db_session.flush()
    await br._settle_resource(db_session, resource, {("series", s2.id)})
    assert resource.batch_seasons is None


async def test_settle_resource_no_seasons_leaves_null(db_session):
    resource = await _make_resource(
        db_session, is_batch=True, batch_scope=None,
    )
    await br._settle_resource(db_session, resource, set())
    assert resource.batch_seasons is None


# ---------------------------------------------------------------------------
# expand_bangumi_series_graph — fake Bangumi graph (Initial D shape)
# ---------------------------------------------------------------------------


_DETAILS = {
    2: {"id": 2, "name": "頭文字D Second Stage", "name_cn": "头文字D Second Stage",
        "date": "1999-10-15", "platform": "TV", "eps": 13, "summary": "s2"},
    3: {"id": 3, "name": "頭文字D Third Stage", "name_cn": "头文字D Third Stage",
        "date": "2001-01-13", "platform": "剧场版", "summary": "third"},
    4: {"id": 4, "name": "頭文字D Extra Stage", "name_cn": "头文字D Extra Stage",
        "date": "2000-02-21", "platform": "OVA", "eps": 2, "summary": "extra"},
    5: {"id": 5, "name": "頭文字D Fourth Stage", "name_cn": "头文字D Fourth Stage",
        "date": "2004-04-17", "platform": "TV", "eps": 24, "summary": "s4"},
    6: {"id": 6, "name": "頭文字D Fifth Stage", "name_cn": "头文字D Fifth Stage",
        "date": "2012-11-04", "platform": "TV", "eps": 14, "summary": "s5"},
    7: {"id": 7, "name": "新劇場版 頭文字D Legend1", "name_cn": "头文字D Legend1",
        "date": "2014-08-23", "platform": "剧场版", "summary": "legend1"},
    8: {"id": 8, "name": "頭文字D Final Stage", "name_cn": "头文字D Final Stage",
        "date": "2014-05-16", "platform": "TV", "eps": 4, "summary": "final"},
}

_RELATIONS = {
    1: [
        {"id": 2, "type": 2, "relation": "续集", "name": "頭文字D Second Stage",
         "name_cn": "头文字D Second Stage", "date": "1999-10-15"},
        {"id": 3, "type": 2, "relation": "剧场版", "name": "頭文字D Third Stage",
         "name_cn": "头文字D Third Stage", "date": "2001-01-13"},
        {"id": 4, "type": 2, "relation": "番外篇", "name": "頭文字D Extra Stage",
         "name_cn": "头文字D Extra Stage", "date": "2000-02-21"},
        {"id": 90, "type": 3, "relation": "片头曲", "name": "OP"},
        {"id": 91, "type": 2, "relation": "相同世界观", "name": "MF Ghost"},
    ],
    2: [
        {"id": 1, "type": 2, "relation": "前传", "name": "頭文字D"},
        {"id": 5, "type": 2, "relation": "续集", "name": "頭文字D Fourth Stage",
         "name_cn": "頭文字D Fourth Stage", "date": "2004-04-17"},
    ],
    5: [
        {"id": 6, "type": 2, "relation": "续集", "name": "頭文字D Fifth Stage",
         "name_cn": "頭文字D Fifth Stage", "date": "2012-11-04"},
        {"id": 7, "type": 2, "relation": "不同演绎", "name": "新劇場版 頭文字D Legend1",
         "name_cn": "头文字D Legend1", "date": "2014-08-23"},
    ],
    6: [
        {"id": 8, "type": 2, "relation": "续集", "name": "頭文字D Final Stage",
         "name_cn": "頭文字D Final Stage", "date": "2014-05-16"},
    ],
}


class _FakeAsyncClient:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


async def _episodes_stub(client, sid):
    return [{"sort": 1, "ep": 1, "name": "ep1", "airdate": "2000-01-01"}]


def _install_bangumi_mock(monkeypatch, *, relations=None, relations_exc=None, details=None):
    relations = _RELATIONS if relations is None else relations
    details = _DETAILS if details is None else details

    async def _relations(client, sid):
        if relations_exc is not None:
            raise relations_exc
        return relations.get(int(sid), [])

    async def _detail(client, sid):
        return details.get(int(sid), {})

    async def _episodes(client, sid):
        return await _episodes_stub(client, sid)

    monkeypatch.setattr(br, "bangumi_configured", lambda: True)
    monkeypatch.setattr(br, "get_subject_relations", _relations)
    monkeypatch.setattr(br, "get_subject", _detail)
    monkeypatch.setattr(mb, "get_subject", _detail)
    monkeypatch.setattr(mb, "get_subject_episodes", _episodes)
    monkeypatch.setattr(br.httpx, "AsyncClient", _FakeAsyncClient)


async def _make_seed_work(db_session, *, subject_id: int = 1):
    collection = _collection("头文字D")
    work = _series(
        "头文字D", season=1, subj=subject_id, collection_id=collection.id,
        original_title="頭文字D First Stage", start_date=date(1998, 4, 18),
    )
    db_session.add_all([collection, work])
    await db_session.flush()
    return collection, work


async def _works_by_bangumi_id(db_session):
    series = {
        w.external_id: w for w in (
            await db_session.execute(
                select(TVSeries).where(TVSeries.external_source == "bangumi")
            )
        ).scalars().all()
    }
    movies = {
        w.external_id: w for w in (
            await db_session.execute(
                select(Movie).where(Movie.external_source == "bangumi")
            )
        ).scalars().all()
    }
    return series, movies


# ---------------------------------------------------------------------------
# expand_bangumi_series_graph — early returns
# ---------------------------------------------------------------------------


async def test_expand_non_batch_and_wrong_scope_are_noops(db_session, monkeypatch):
    called: list = []

    async def _relations(client, sid):  # pragma: no cover - must not be called
        called.append(sid)
        return []

    monkeypatch.setattr(br, "bangumi_configured", lambda: True)
    monkeypatch.setattr(br, "get_subject_relations", _relations)
    collection, seed = await _make_seed_work(db_session)

    non_batch = await _make_resource(
        db_session, is_batch=False, batch_scope=None, series_id=seed.id,
    )
    assert await br.expand_bangumi_series_graph(db_session, non_batch) == 0
    season_scope = await _make_resource(
        db_session, is_batch=True, batch_scope="season", series_id=seed.id,
    )
    assert await br.expand_bangumi_series_graph(db_session, season_scope) == 0
    assert called == []


async def test_expand_not_configured_is_noop(db_session, monkeypatch):
    monkeypatch.setattr(br, "bangumi_configured", lambda: False)
    collection, seed = await _make_seed_work(db_session)
    resource = await _make_resource(
        db_session, is_batch=True, batch_scope="multi_season", series_id=seed.id,
    )
    assert await br.expand_bangumi_series_graph(db_session, resource) == 0
    assert await _links(db_session, resource.id) == []


async def test_expand_no_seeds_is_noop(db_session, monkeypatch):
    monkeypatch.setattr(br, "bangumi_configured", lambda: True)
    resource = await _make_resource(
        db_session, is_batch=True, batch_scope="multi_season",
    )
    assert await br.expand_bangumi_series_graph(db_session, resource) == 0


async def test_expand_no_target_collection_is_noop(db_session, monkeypatch):
    monkeypatch.setattr(br, "bangumi_configured", lambda: True)
    seed = _series("头文字D", season=1, subj=1, collection_id=None)
    db_session.add(seed)
    await db_session.flush()
    resource = await _make_resource(
        db_session, is_batch=True, batch_scope="multi_season",
        series_id=seed.id, collection_id=None,
    )
    assert await br.expand_bangumi_series_graph(db_session, resource) == 0


async def test_expand_missing_target_collection_row_is_noop(db_session, monkeypatch):
    monkeypatch.setattr(br, "bangumi_configured", lambda: True)
    phantom = _collection("phantom")
    seed = _series("头文字D", season=1, subj=1, collection_id=None)
    db_session.add_all([phantom, seed])
    await db_session.flush()
    resource = await _make_resource(
        db_session, is_batch=True, batch_scope="multi_season",
        series_id=seed.id, collection_id=phantom.id,
    )

    real_get = db_session.get

    async def _get(model, ident, *args, **kwargs):
        if model is WorkCollection:
            return None
        return await real_get(model, ident, *args, **kwargs)

    monkeypatch.setattr(db_session, "get", _get)
    assert await br.expand_bangumi_series_graph(db_session, resource) == 0


async def test_expand_relations_failure_keeps_original_verdict(db_session, monkeypatch):
    _install_bangumi_mock(monkeypatch, relations_exc=TimeoutError("boom"))
    collection, seed = await _make_seed_work(db_session)
    resource = await _make_resource(
        db_session, is_batch=True, batch_scope="multi_season",
        series_id=seed.id, batch_seasons=[1, 2, 3, 4, 5, 6],
    )
    assert await br.expand_bangumi_series_graph(db_session, resource) == 0
    series, movies = await _works_by_bangumi_id(db_session)
    assert set(series) == {"bangumi:1"}
    assert movies == {}
    assert await _links(db_session, resource.id) == []


# ---------------------------------------------------------------------------
# expand_bangumi_series_graph — full graph, gates, two-phase admission
# ---------------------------------------------------------------------------


async def test_expand_multi_season_full_graph(db_session, monkeypatch):
    _install_bangumi_mock(monkeypatch)
    collection, seed = await _make_seed_work(db_session)
    resource = await _make_resource(
        db_session, is_batch=True, batch_scope="multi_season",
        series_id=seed.id, batch_seasons=[1, 2, 3, 4, 5, 6],
    )

    created = await br.expand_bangumi_series_graph(db_session, resource)
    await db_session.commit()

    series, movies = await _works_by_bangumi_id(db_session)
    assert series["bangumi:2"].season_number == 2
    assert series["bangumi:5"].season_number == 4
    assert series["bangumi:6"].season_number == 5
    assert series["bangumi:8"].season_number == 6
    assert series["bangumi:4"].season_number == 0
    assert movies["bangumi:3"].content_type == "movie"
    assert movies["bangumi:7"].collection_id == collection.id
    links = await _links(db_session, resource.id)
    assert {link.series_id or link.movie_id for link in links} == {
        seed.id, series["bangumi:2"].id, series["bangumi:4"].id, series["bangumi:5"].id,
        series["bangumi:6"].id, series["bangumi:8"].id, movies["bangumi:3"].id,
        movies["bangumi:7"].id,
    }
    assert created == 8
    await db_session.refresh(resource)
    assert resource.batch_seasons == [0, 1, 2, 3, 4, 5, 6]


async def test_expand_is_idempotent(db_session, monkeypatch):
    _install_bangumi_mock(monkeypatch)
    collection, seed = await _make_seed_work(db_session)
    resource = await _make_resource(
        db_session, is_batch=True, batch_scope="multi_season", series_id=seed.id,
    )
    first = await br.expand_bangumi_series_graph(db_session, resource)
    await db_session.commit()
    second = await br.expand_bangumi_series_graph(db_session, resource)
    await db_session.commit()
    assert first > 0 and second == 0
    assert len(await _links(db_session, resource.id)) == first


async def test_expand_franchise_seeds_from_collection_members(db_session, monkeypatch):
    _install_bangumi_mock(monkeypatch)
    collection, seed = await _make_seed_work(db_session)
    resource = await _make_resource(
        db_session, is_batch=True, batch_scope="franchise",
        series_id=None, collection_id=collection.id,
    )
    created = await br.expand_bangumi_series_graph(db_session, resource)
    await db_session.commit()
    series, movies = await _works_by_bangumi_id(db_session)
    assert series["bangumi:8"].season_number == 6
    assert movies["bangumi:3"].collection_id == collection.id
    assert created == 8


async def test_expand_skips_bad_relation_entries(db_session, monkeypatch):
    relations = {
        1: [
            {"id": "not-an-int", "type": 2, "relation": "续集", "name": "x"},
            {"id": 2, "type": 3, "relation": "续集", "name": "頭文字D Second Stage"},
            {"id": 3, "type": 2, "relation": "未知关系", "name": "y"},
            {"id": 4, "type": 2, "relation": "续集", "name": "頭文字D Second Stage",
             "name_cn": "头文字D Second Stage", "date": "1999-10-15"},
            {"id": 4, "type": 2, "relation": "续集", "name": "dup"},
        ],
        4: [
            {"id": 1, "type": 2, "relation": "前传", "name": "頭文字D"},
        ],
    }
    _install_bangumi_mock(monkeypatch, relations=relations)
    collection, seed = await _make_seed_work(db_session)
    resource = await _make_resource(
        db_session, is_batch=True, batch_scope="multi_season", series_id=seed.id,
    )
    await br.expand_bangumi_series_graph(db_session, resource)
    await db_session.commit()
    series, _ = await _works_by_bangumi_id(db_session)
    assert "bangumi:4" in series
    assert "bangumi:2" not in series
    assert "bangumi:3" not in series


async def test_expand_detail_failure_skips_node(db_session, monkeypatch):
    relations = {
        1: [
            {"id": 2, "type": 2, "relation": "续集", "name": "頭文字D Second Stage",
             "name_cn": "头文字D Second Stage", "date": "1999-10-15"},
        ],
    }

    async def _detail(client, sid):
        if int(sid) == 2:
            raise TimeoutError("detail boom")
        return _DETAILS.get(int(sid), {})

    _install_bangumi_mock(monkeypatch, relations=relations)
    monkeypatch.setattr(br, "get_subject", _detail)
    monkeypatch.setattr(mb, "get_subject", _detail)
    collection, seed = await _make_seed_work(db_session)
    resource = await _make_resource(
        db_session, is_batch=True, batch_scope="multi_season", series_id=seed.id,
    )
    await br.expand_bangumi_series_graph(db_session, resource)
    await db_session.commit()
    series, _ = await _works_by_bangumi_id(db_session)
    assert "bangumi:2" not in series


async def test_expand_skips_unknown_shape(db_session, monkeypatch):
    relations = {
        1: [
            {"id": 2, "type": 2, "relation": "不同演绎", "name": "頭文字D 限定",
             "name_cn": "头文字D 限定", "date": "2010-01-01"},
        ],
    }
    details = dict(_DETAILS)
    details[2] = {"id": 2, "name": "頭文字D 限定", "name_cn": "头文字D 限定",
                  "date": "2010-01-01", "platform": "TV", "summary": "tv adaptation"}
    _install_bangumi_mock(monkeypatch, relations=relations, details=details)
    collection, seed = await _make_seed_work(db_session)
    resource = await _make_resource(
        db_session, is_batch=True, batch_scope="multi_season", series_id=seed.id,
    )
    await br.expand_bangumi_series_graph(db_session, resource)
    await db_session.commit()
    series, movies = await _works_by_bangumi_id(db_session)
    assert "bangumi:2" not in series
    assert "bangumi:2" not in movies


async def test_expand_cross_ip_sequel_rejected_and_not_traversed(db_session, monkeypatch):
    fetched: list = []
    relations = {
        1: [
            {"id": 8, "type": 2, "relation": "续集", "name": "頭文字D Final Stage",
             "name_cn": "头文字D Final Stage", "date": "2014-05-16"},
        ],
        8: [
            {"id": 20, "type": 2, "relation": "续集", "name": "MFゴースト",
             "name_cn": "极速车魂", "date": "2023-09-30"},
        ],
        20: [
            {"id": 21, "type": 2, "relation": "续集", "name": "MFゴースト 2nd Season",
             "name_cn": "极速车魂 第二季", "date": "2024-10-06"},
        ],
    }
    details = dict(_DETAILS)
    details[20] = {"id": 20, "name": "MFゴースト", "name_cn": "极速车魂",
                   "date": "2023-09-30", "platform": "TV", "summary": "mfg"}
    details[21] = {"id": 21, "name": "MFゴースト 2nd Season", "name_cn": "极速车魂 第二季",
                   "date": "2024-10-06", "platform": "TV", "summary": "mfg2"}

    async def _relations(client, sid):
        fetched.append(int(sid))
        return relations.get(int(sid), [])

    _install_bangumi_mock(monkeypatch, relations=relations, details=details)
    monkeypatch.setattr(br, "get_subject_relations", _relations)
    collection, seed = await _make_seed_work(db_session)
    resource = await _make_resource(
        db_session, is_batch=True, batch_scope="multi_season", series_id=seed.id,
    )
    await br.expand_bangumi_series_graph(db_session, resource)
    await db_session.commit()
    series, _ = await _works_by_bangumi_id(db_session)
    assert series["bangumi:8"].season_number == 2
    assert "bangumi:20" not in series
    assert 20 not in fetched


async def test_expand_second_specials_subject_gets_own_shell(db_session, monkeypatch):
    relations = {
        1: [
            {"id": 4, "type": 2, "relation": "番外篇", "name": "頭文字D Extra Stage",
             "name_cn": "頭文字D Extra Stage", "date": "2000-02-21"},
            {"id": 10, "type": 2, "relation": "番外篇", "name": "頭文字D Battle Stage",
             "name_cn": "頭文字D Battle Stage", "date": "2002-05-15"},
        ],
    }
    details = dict(_DETAILS)
    details[10] = {"id": 10, "name": "頭文字D Battle Stage",
                   "name_cn": "頭文字D Battle Stage", "date": "2002-05-15",
                   "platform": "OVA", "summary": "battle"}
    _install_bangumi_mock(monkeypatch, relations=relations, details=details)
    collection, seed = await _make_seed_work(db_session)
    resource = await _make_resource(
        db_session, is_batch=True, batch_scope="multi_season", series_id=seed.id,
    )
    await br.expand_bangumi_series_graph(db_session, resource)
    await db_session.commit()
    series, _ = await _works_by_bangumi_id(db_session)
    assert series["bangumi:4"].season_number == 0
    second = series["bangumi:10"]
    assert second.season_number == 1
    assert second.collection_id != collection.id
    shell = await db_session.get(WorkCollection, second.collection_id)
    assert shell is not None and shell.external_source == "series_group"


async def test_expand_same_name_auto_collection_absorbed(db_session, monkeypatch):
    _install_bangumi_mock(monkeypatch)
    shell = _collection("头文字D")
    first = _series("头文字D", season=1, subj=1, collection_id=shell.id,
                    original_title="頭文字D", start_date=date(1998, 4, 18))
    second = _series("头文字D Second Stage", season=2, subj=2, collection_id=shell.id,
                     original_title="頭文字D Second Stage", start_date=date(1999, 10, 15))
    pack = _collection("头文字D Initial D", source="franchise_pack")
    db_session.add_all([shell, first, second, pack])
    await db_session.flush()
    resource = await _make_resource(
        db_session, is_batch=True, batch_scope="franchise", collection_id=pack.id,
    )
    db_session.add(ResourceWorkLink(resource_id=resource.id, series_id=first.id, source="auto"))
    await db_session.flush()

    await br.expand_bangumi_series_graph(db_session, resource)
    await db_session.commit()

    assert await db_session.get(WorkCollection, shell.id) is None
    await db_session.refresh(first)
    await db_session.refresh(second)
    assert first.collection_id == pack.id
    assert second.collection_id == pack.id


async def test_expand_manual_same_name_collection_not_absorbed(db_session, monkeypatch):
    _install_bangumi_mock(monkeypatch)
    shell = _collection("头文字D", manually_edited_fields=["title_cn"])
    first = _series("头文字D", season=1, subj=1, collection_id=shell.id,
                    original_title="頭文字D", start_date=date(1998, 4, 18))
    second = _series("头文字D Second Stage", season=2, subj=2, collection_id=shell.id,
                     original_title="頭文字D Second Stage", start_date=date(1999, 10, 15))
    pack = _collection("头文字D Initial D", source="franchise_pack")
    db_session.add_all([shell, first, second, pack])
    await db_session.flush()
    resource = await _make_resource(
        db_session, is_batch=True, batch_scope="franchise", collection_id=pack.id,
    )
    db_session.add(ResourceWorkLink(resource_id=resource.id, series_id=first.id, source="auto"))
    await db_session.flush()

    await br.expand_bangumi_series_graph(db_session, resource)
    await db_session.commit()

    assert await db_session.get(WorkCollection, shell.id) is not None
    await db_session.refresh(second)
    assert second.collection_id == shell.id
    links = await _links(db_session, resource.id)
    assert second.id not in {link.series_id for link in links}


async def test_expand_phase_a_correction_frees_slot(db_session, monkeypatch):
    relations = {
        1: [
            {"id": 6, "type": 2, "relation": "续集", "name": "頭文字D Fifth Stage",
             "name_cn": "頭文字D Fifth Stage", "date": "2012-11-04"},
        ],
        6: [
            {"id": 8, "type": 2, "relation": "续集", "name": "頭文字D Final Stage",
             "name_cn": "頭文字D Final Stage", "date": "2014-05-16"},
        ],
    }
    _install_bangumi_mock(monkeypatch, relations=relations)
    collection, seed = await _make_seed_work(db_session)
    misplaced = _series(
        "头文字D Final Stage", season=5, subj=8, collection_id=collection.id,
        original_title="頭文字D Final Stage", start_date=date(2014, 5, 16),
    )
    db_session.add(misplaced)
    await db_session.flush()
    db_session.add(WorkExternalId(
        work_type="series", work_id=misplaced.id, source="bangumi", external_id="bangumi:8",
    ))
    await db_session.flush()
    resource = await _make_resource(
        db_session, is_batch=True, batch_scope="multi_season",
        series_id=seed.id, batch_seasons=[1],
    )

    created = await br.expand_bangumi_series_graph(db_session, resource)
    await db_session.commit()
    series, _ = await _works_by_bangumi_id(db_session)
    assert series["bangumi:8"].season_number == 6
    assert series["bangumi:6"].season_number == 5
    assert created >= 3


async def test_expand_movie_upsert_failure_is_swallowed(db_session, monkeypatch):
    import app.services.metadata_service as ms

    _install_bangumi_mock(monkeypatch)

    async def _boom(db, entity, **kwargs):
        raise RuntimeError("movie upsert boom")

    monkeypatch.setattr(ms, "create_or_update_movie_from_external", _boom)
    collection, seed = await _make_seed_work(db_session)
    resource = await _make_resource(
        db_session, is_batch=True, batch_scope="multi_season", series_id=seed.id,
    )
    created = await br.expand_bangumi_series_graph(db_session, resource)
    await db_session.commit()
    series, movies = await _works_by_bangumi_id(db_session)
    assert movies == {}
    assert "bangumi:2" in series
    assert created > 0


async def test_expand_member_reuse_bags_identity(db_session, monkeypatch):
    """Phase B reuses an existing (collection, season) member and bags its id."""
    _install_bangumi_mock(monkeypatch)
    collection, seed = await _make_seed_work(db_session)
    # A pre-existing member for season 2 with no bangumi identity at all.
    member = _series("头文字D Second Stage", season=2, source="manual",
                     collection_id=collection.id)
    db_session.add(member)
    await db_session.flush()
    resource = await _make_resource(
        db_session, is_batch=True, batch_scope="multi_season", series_id=seed.id,
    )
    await br.expand_bangumi_series_graph(db_session, resource)
    await db_session.commit()

    bag = (
        await db_session.execute(
            select(WorkExternalId).where(
                WorkExternalId.work_id == member.id,
                WorkExternalId.source == "bangumi",
            )
        )
    ).scalars().all()
    assert {b.external_id for b in bag} == {"bangumi:2"}
    links = await _links(db_session, resource.id)
    assert member.id in {link.series_id for link in links}


async def test_expand_phase_b_corrects_stale_upsert_season(db_session, monkeypatch):
    """A same-pass upsert returning the wrong season is retried by phase B."""
    import app.services.metadata_service as ms

    _install_bangumi_mock(monkeypatch)
    collection, seed = await _make_seed_work(db_session)

    async def _stale(db, entity, *, season_hint=None):
        work = _series("stale", season=1, source="bangumi",
                       collection_id=collection.id)
        work.external_id = entity.get("external_id")
        work.external_source = "bangumi"
        db.add(work)
        await db.flush()
        return work

    monkeypatch.setattr(ms, "create_or_update_series_from_external", _stale)
    # Only one sequel node so the wrong season (1) collides with the seed.
    relations = {
        1: [
            {"id": 2, "type": 2, "relation": "续集", "name": "頭文字D Second Stage",
             "name_cn": "头文字D Second Stage", "date": "1999-10-15"},
        ],
    }
    _install_bangumi_mock(monkeypatch, relations=relations)
    resource = await _make_resource(
        db_session, is_batch=True, batch_scope="multi_season", series_id=seed.id,
    )
    created = await br.expand_bangumi_series_graph(db_session, resource)
    await db_session.commit()
    series, _ = await _works_by_bangumi_id(db_session)
    # The stale work was corrected to season 2 and linked.
    assert series["bangumi:2"].season_number == 2
    assert created >= 2


async def test_expand_graph_node_cap_stops_bfs(db_session, monkeypatch):
    """The graph-walk cap bounds API calls (line 616)."""
    relations = {
        1: [
            {"id": 2, "type": 2, "relation": "续集", "name": "頭文字D Second Stage",
             "name_cn": "頭文字D Second Stage", "date": "1999-10-15"},
        ],
        2: [
            {"id": 5, "type": 2, "relation": "续集", "name": "頭文字D Fourth Stage",
             "name_cn": "頭文字D Fourth Stage", "date": "2004-04-17"},
        ],
    }
    _install_bangumi_mock(monkeypatch, relations=relations)
    monkeypatch.setattr(br, "_MAX_GRAPH_NODES", 2)
    collection, seed = await _make_seed_work(db_session)
    resource = await _make_resource(
        db_session, is_batch=True, batch_scope="multi_season", series_id=seed.id,
    )
    await br.expand_bangumi_series_graph(db_session, resource)
    await db_session.commit()
    series, _ = await _works_by_bangumi_id(db_session)
    assert "bangumi:2" in series
    assert "bangumi:5" not in series


async def test_expand_member_reuse_different_subject_falls_through(db_session, monkeypatch):
    """A different Bangumi subject occupying the slot is never merged (line 697)."""
    _install_bangumi_mock(monkeypatch)
    collection, seed = await _make_seed_work(db_session)
    member = _series("other subject", season=2, subj=99, collection_id=collection.id)
    db_session.add(member)
    await db_session.flush()
    resource = await _make_resource(
        db_session, is_batch=True, batch_scope="multi_season", series_id=seed.id,
    )
    await br.expand_bangumi_series_graph(db_session, resource)
    await db_session.commit()
    series, _ = await _works_by_bangumi_id(db_session)
    # The existing member keeps its identity and is never merged with node 2;
    # the conflicting incoming identity is parked on the collection instead.
    assert series["bangumi:99"].id == member.id
    assert "bangumi:2" not in series
    await db_session.refresh(member)
    assert member.season_number == 2


async def test_expand_season_node_deferred_to_movie_pass(db_session, monkeypatch):
    """A season-classified node whose entity turns out movie-form is deferred
    to the movie pass (lines 704, 816-819)."""
    _install_bangumi_mock(monkeypatch)

    async def _movie_build(client, subject, *, season, detail=None):
        return {
            "external_id": f"bangumi:{subject['id']}",
            "external_source": "bangumi",
            "title_cn": subject.get("name_cn") or subject.get("name"),
            "original_title": subject.get("name"),
            "_content_type": "movie",
        }

    monkeypatch.setattr(mb, "_build_matched_entity", _movie_build)
    relations = {
        1: [
            {"id": 2, "type": 2, "relation": "续集", "name": "頭文字D Second Stage",
             "name_cn": "頭文字D Second Stage", "date": "1999-10-15"},
        ],
    }
    _install_bangumi_mock(monkeypatch, relations=relations)
    monkeypatch.setattr(mb, "_build_matched_entity", _movie_build)
    collection, seed = await _make_seed_work(db_session)
    resource = await _make_resource(
        db_session, is_batch=True, batch_scope="multi_season", series_id=seed.id,
    )
    await br.expand_bangumi_series_graph(db_session, resource)
    await db_session.commit()
    series, movies = await _works_by_bangumi_id(db_session)
    assert "bangumi:2" not in series
    assert "bangumi:2" in movies


async def test_expand_phase_b_correction_failure_skips_linking(db_session, monkeypatch):
    """When phase B correction cannot apply, the node is skipped (line 711)."""
    import app.services.metadata_service as ms

    _install_bangumi_mock(monkeypatch)
    collection, seed = await _make_seed_work(db_session)
    # Pre-occupy season 2 with a different subject so nothing can move there.
    occupant = _series("occupant", season=2, subj=77, collection_id=collection.id)
    db_session.add(occupant)
    await db_session.flush()

    async def _stale_manual(db, entity, *, season_hint=None):
        work = _series("stale", season=1, source="bangumi",
                       collection_id=collection.id,
                       manually_edited_fields=["season_number"])
        work.external_id = entity.get("external_id")
        work.external_source = "bangumi"
        db.add(work)
        await db_session.flush()
        return work

    monkeypatch.setattr(ms, "create_or_update_series_from_external", _stale_manual)
    relations = {
        1: [
            {"id": 2, "type": 2, "relation": "续集", "name": "頭文字D Second Stage",
             "name_cn": "头文字D Second Stage", "date": "1999-10-15"},
        ],
    }
    _install_bangumi_mock(monkeypatch, relations=relations)
    resource = await _make_resource(
        db_session, is_batch=True, batch_scope="multi_season", series_id=seed.id,
    )
    await br.expand_bangumi_series_graph(db_session, resource)
    await db_session.commit()
    links = await _links(db_session, resource.id)
    linked = {link.series_id for link in links}
    # The manually-edited stale work is never linked.
    stale = (
        await db_session.execute(
            select(TVSeries).where(TVSeries.external_id == "bangumi:2")
        )
    ).scalars().first()
    assert stale is not None and stale.id not in linked


async def test_expand_specials_upsert_returning_none_skipped(db_session, monkeypatch):
    """A failed specials upsert leaves the node unlinked (line 735)."""
    import app.services.metadata_service as ms

    _install_bangumi_mock(monkeypatch)

    async def _none(db, entity, *, season_hint=None):
        return None

    monkeypatch.setattr(ms, "create_or_update_series_from_external", _none)
    relations = {
        1: [
            {"id": 4, "type": 2, "relation": "番外篇", "name": "頭文字D Extra Stage",
             "name_cn": "頭文字D Extra Stage", "date": "2000-02-21"},
        ],
    }
    _install_bangumi_mock(monkeypatch, relations=relations)
    collection, seed = await _make_seed_work(db_session)
    resource = await _make_resource(
        db_session, is_batch=True, batch_scope="multi_season", series_id=seed.id,
    )
    await br.expand_bangumi_series_graph(db_session, resource)
    await db_session.commit()
    series, _ = await _works_by_bangumi_id(db_session)
    assert "bangumi:4" not in series


async def test_expand_second_specials_upsert_none_skipped(db_session, monkeypatch):
    """The shell-specials branch skips a failed second upsert (line 750)."""
    import app.services.metadata_service as ms

    _install_bangumi_mock(monkeypatch)
    real_create = ms.create_or_update_series_from_external

    async def _none_second(db, entity, *, season_hint=None):
        if season_hint == 1:
            return None
        return await real_create(db, entity, season_hint=season_hint)

    monkeypatch.setattr(ms, "create_or_update_series_from_external", _none_second)
    relations = {
        1: [
            {"id": 4, "type": 2, "relation": "番外篇", "name": "頭文字D Extra Stage",
             "name_cn": "頭文字D Extra Stage", "date": "2000-02-21"},
            {"id": 10, "type": 2, "relation": "番外篇", "name": "頭文字D Battle Stage",
             "name_cn": "頭文字D Battle Stage", "date": "2002-05-15"},
        ],
    }
    details = dict(_DETAILS)
    details[10] = {"id": 10, "name": "頭文字D Battle Stage",
                   "name_cn": "頭文字D Battle Stage", "date": "2002-05-15",
                   "platform": "OVA", "summary": "battle"}
    _install_bangumi_mock(monkeypatch, relations=relations, details=details)
    monkeypatch.setattr(ms, "create_or_update_series_from_external", _none_second)
    collection, seed = await _make_seed_work(db_session)
    resource = await _make_resource(
        db_session, is_batch=True, batch_scope="multi_season", series_id=seed.id,
    )
    await br.expand_bangumi_series_graph(db_session, resource)
    await db_session.commit()
    series, _ = await _works_by_bangumi_id(db_session)
    assert "bangumi:4" in series
    assert "bangumi:10" not in series
