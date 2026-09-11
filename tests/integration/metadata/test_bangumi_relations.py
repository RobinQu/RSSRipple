"""Integration tests for app.services.bangumi_relations (B1 series graph).

All Bangumi HTTP is mocked at the module boundary
(``br.get_subject_relations`` / ``br.get_subject`` for the graph walk and
``mb.get_subject`` / ``mb.get_subject_episodes`` for entity building). The
scenario mirrors the real 头文字D layout: First Stage (seed, season 1) with
续集 Second→Fourth→Fifth→Final Stage, 剧场版 Third Stage, 番外篇 Extra Stage
and 不同演绎 Legend1 (movie-form).
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import select

import app.services.bangumi_relations as br
import app.services.metadata_bangumi as mb
from app.models.channel import Channel
from app.models.file_resource import FileResource
from app.models.movie import Movie
from app.models.resource_work_link import ResourceWorkLink
from app.models.series import TVSeries
from app.models.work_collection import WorkCollection
from app.models.work_external_id import WorkExternalId


def _uuid() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Fake Bangumi graph (Initial D shape)
# ---------------------------------------------------------------------------

# sid → detail response (platform drives the movie/TV split)
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
        # Non-anime relation (a music entry) — ignored.
        {"id": 90, "type": 3, "relation": "片头曲", "name": "OP"},
        # Unknown relation label — ignored.
        {"id": 91, "type": 2, "relation": "相同世界观", "name": "MF Ghost"},
    ],
    2: [
        {"id": 1, "type": 2, "relation": "前传", "name": "頭文字D"},
        {"id": 5, "type": 2, "relation": "续集", "name": "頭文字D Fourth Stage",
         "name_cn": "头文字D Fourth Stage", "date": "2004-04-17"},
    ],
    5: [
        {"id": 6, "type": 2, "relation": "续集", "name": "頭文字D Fifth Stage",
         "name_cn": "头文字D Fifth Stage", "date": "2012-11-04"},
        {"id": 7, "type": 2, "relation": "不同演绎", "name": "新劇場版 頭文字D Legend1",
         "name_cn": "头文字D Legend1", "date": "2014-08-23"},
    ],
    6: [
        {"id": 8, "type": 2, "relation": "续集", "name": "頭文字D Final Stage",
         "name_cn": "头文字D Final Stage", "date": "2014-05-16"},
    ],
}


def _install_bangumi_mock(monkeypatch, *, relations=None, relations_exc=None):
    """Mock every Bangumi call the graph pass can make."""
    relations = _RELATIONS if relations is None else relations

    async def _relations(client, sid):
        if relations_exc is not None:
            raise relations_exc
        return relations.get(int(sid), [])

    async def _detail(client, sid):
        return _DETAILS.get(int(sid), {})

    async def _episodes(client, sid):
        return [
            {"sort": 1, "ep": 1, "name": "ep1", "airdate": "2000-01-01"},
            {"sort": 2, "ep": 2, "name": "ep2", "airdate": "2000-01-08"},
        ]

    monkeypatch.setattr(br, "bangumi_configured", lambda: True)
    monkeypatch.setattr(br, "get_subject_relations", _relations)
    monkeypatch.setattr(br, "get_subject", _detail)
    monkeypatch.setattr(mb, "get_subject", _detail)
    monkeypatch.setattr(mb, "get_subject_episodes", _episodes)


async def _make_resource(db_session, **over):
    channel = Channel(
        id=_uuid(),
        name="Bangumi Graph Channel",
        type="rss_feed",
        url="https://example.com/rss",
        fetch_interval=1800,
        status="active",
        field_mapping={"list_locator": {"source": "entries"}},
        metadata_agent_enabled=False,
    )
    base = dict(
        id=_uuid(),
        channel_id=channel.id,
        guid=_uuid(),
        title_raw="头文字D.全六季.日语.英文字幕.Initial D",
        torrent_url="https://x/pack.torrent",
    )
    base.update(over)
    resource = FileResource(**base)
    db_session.add_all([channel, resource])
    await db_session.commit()
    return resource


async def _make_seed_work(db_session, *, subject_id: int = 1):
    """The First Stage season-1 work the pack linked to + its shell collection."""
    collection = WorkCollection(
        id=_uuid(), title_cn="头文字D",
        external_id=None, external_source="series_group",
    )
    work = TVSeries(
        id=_uuid(),
        title_cn="头文字D",
        original_title="頭文字D First Stage",
        external_source="bangumi",
        external_id=f"bangumi:{subject_id}",
        content_type="tv",
        season_number=1,
        start_date=date(1998, 4, 18),
        collection_id=collection.id,
    )
    db_session.add_all([collection, work])
    await db_session.commit()
    return collection, work


async def _links(db_session, resource_id):
    return (await db_session.execute(
        select(ResourceWorkLink).where(ResourceWorkLink.resource_id == resource_id)
    )).scalars().all()


async def _works_by_bangumi_id(db_session):
    series = {
        w.external_id: w for w in (await db_session.execute(
            select(TVSeries).where(TVSeries.external_source == "bangumi")
        )).scalars().all()
    }
    movies = {
        w.external_id: w for w in (await db_session.execute(
            select(Movie).where(Movie.external_source == "bangumi")
        )).scalars().all()
    }
    return series, movies


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_multi_season_pack_expands_full_graph(db_session, monkeypatch):
    _install_bangumi_mock(monkeypatch)
    collection, seed = await _make_seed_work(db_session)
    resource = await _make_resource(
        db_session,
        is_batch=True, batch_scope="multi_season",
        series_id=seed.id, batch_seasons=[1, 2, 3, 4, 5, 6],
    )

    created = await br.expand_bangumi_series_graph(db_session, resource)
    await db_session.commit()

    series, movies = await _works_by_bangumi_id(db_session)
    # Stage ordinals pin seasons 2/4/5; Final Stage is air-date-inferred to 6.
    assert series["bangumi:2"].season_number == 2
    assert series["bangumi:5"].season_number == 4
    assert series["bangumi:6"].season_number == 5
    assert series["bangumi:8"].season_number == 6
    # 番外篇 → the collection's season-0 specials work.
    assert series["bangumi:4"].season_number == 0
    # Every expanded season work sits in the seed's collection.
    for sid in ("bangumi:2", "bangumi:4", "bangumi:5", "bangumi:6", "bangumi:8"):
        assert series[sid].collection_id == collection.id
    # 剧场版 Third Stage + movie-form 不同演绎 Legend1 → Movie works.
    assert movies["bangumi:3"].collection_id == collection.id
    assert movies["bangumi:7"].collection_id == collection.id
    assert movies["bangumi:3"].content_type == "movie"

    # Links: five season works + two movies + the seed itself.
    links = await _links(db_session, resource.id)
    link_ids = {(link.series_id or link.movie_id) for link in links}
    expected = {
        seed.id,
        series["bangumi:2"].id, series["bangumi:4"].id, series["bangumi:5"].id,
        series["bangumi:6"].id, series["bangumi:8"].id,
        movies["bangumi:3"].id, movies["bangumi:7"].id,
    }
    assert link_ids == expected
    assert created == len(expected)
    assert all(link.source == "auto" for link in links)

    # Derived state settled from the expanded link set. The title-declared
    # coverage (1..6) unions with the linked season numbers — never shrunk.
    await db_session.refresh(resource)
    assert resource.collection_id == collection.id
    assert resource.batch_seasons == [0, 1, 2, 3, 4, 5, 6]

    # Episode rows landed for an expanded season work.
    from app.models.episode import Episode

    eps = (await db_session.execute(
        select(Episode).where(Episode.series_id == series["bangumi:2"].id)
    )).scalars().all()
    assert {e.episode for e in eps} == {1, 2}
    assert all(e.season == 2 for e in eps)


async def test_expansion_is_idempotent(db_session, monkeypatch):
    _install_bangumi_mock(monkeypatch)
    collection, seed = await _make_seed_work(db_session)
    resource = await _make_resource(
        db_session, is_batch=True, batch_scope="multi_season", series_id=seed.id,
    )

    first = await br.expand_bangumi_series_graph(db_session, resource)
    await db_session.commit()
    second = await br.expand_bangumi_series_graph(db_session, resource)
    await db_session.commit()

    assert first > 0
    assert second == 0
    series, movies = await _works_by_bangumi_id(db_session)
    assert len(series) == 6  # seed + 5 expanded
    assert len(movies) == 2
    assert len(await _links(db_session, resource.id)) == first


async def test_franchise_pack_seeds_from_collection_members(db_session, monkeypatch):
    _install_bangumi_mock(monkeypatch)
    collection, seed = await _make_seed_work(db_session)
    # Franchise packs clear the flat FKs and own their collection directly.
    resource = await _make_resource(
        db_session,
        is_batch=True, batch_scope="franchise",
        series_id=None, collection_id=collection.id,
    )

    created = await br.expand_bangumi_series_graph(db_session, resource)
    await db_session.commit()

    series, movies = await _works_by_bangumi_id(db_session)
    assert series["bangumi:8"].season_number == 6
    assert movies["bangumi:3"].collection_id == collection.id
    assert created == 8
    # Franchise resources own their collection semantics — untouched.
    await db_session.refresh(resource)
    assert resource.collection_id == collection.id


# ---------------------------------------------------------------------------
# Gates & failure behavior
# ---------------------------------------------------------------------------


async def test_non_batch_resource_is_not_expanded(db_session, monkeypatch):
    called = []

    async def _relations(client, sid):
        called.append(sid)
        return []

    monkeypatch.setattr(br, "bangumi_configured", lambda: True)
    monkeypatch.setattr(br, "get_subject_relations", _relations)
    collection, seed = await _make_seed_work(db_session)
    resource = await _make_resource(
        db_session, is_batch=False, batch_scope=None, series_id=seed.id,
    )

    assert await br.expand_bangumi_series_graph(db_session, resource) == 0
    assert called == []


async def test_season_scope_is_not_expanded(db_session, monkeypatch):
    monkeypatch.setattr(br, "bangumi_configured", lambda: True)
    collection, seed = await _make_seed_work(db_session)
    resource = await _make_resource(
        db_session, is_batch=True, batch_scope="season", series_id=seed.id,
    )
    assert await br.expand_bangumi_series_graph(db_session, resource) == 0


async def test_not_configured_is_a_noop(db_session, monkeypatch):
    monkeypatch.setattr(br, "bangumi_configured", lambda: False)
    collection, seed = await _make_seed_work(db_session)
    resource = await _make_resource(
        db_session, is_batch=True, batch_scope="multi_season", series_id=seed.id,
    )
    assert await br.expand_bangumi_series_graph(db_session, resource) == 0
    assert await _links(db_session, resource.id) == []


async def test_relations_failure_keeps_original_verdict(db_session, monkeypatch):
    _install_bangumi_mock(monkeypatch, relations_exc=TimeoutError("boom"))
    collection, seed = await _make_seed_work(db_session)
    resource = await _make_resource(
        db_session,
        is_batch=True, batch_scope="multi_season",
        series_id=seed.id, batch_seasons=[1, 2, 3, 4, 5, 6],
    )

    created = await br.expand_bangumi_series_graph(db_session, resource)
    await db_session.commit()

    assert created == 0
    series, movies = await _works_by_bangumi_id(db_session)
    assert set(series) == {"bangumi:1"}
    assert movies == {}
    # The original link verdict is fully untouched — no works, no links, no
    # resource churn when the relations endpoint fails.
    await db_session.refresh(resource)
    assert resource.series_id == seed.id
    assert resource.batch_seasons == [1, 2, 3, 4, 5, 6]
    assert await _links(db_session, resource.id) == []


async def test_unmarked_sequel_without_ordering_evidence_is_skipped(
    db_session, monkeypatch
):
    """A 续集 with no Stage/season marker and no air date is never guessed."""
    relations = {
        1: [
            {"id": 2, "type": 2, "relation": "续集", "name": "頭文字D Second Stage",
             "name_cn": "头文字D Second Stage", "date": "1999-10-15"},
            {"id": 9, "type": 2, "relation": "续集", "name": "頭文字D 新作",
             "name_cn": "头文字D 新作", "date": None},
        ],
    }
    details = dict(_DETAILS)
    details[9] = {"id": 9, "name": "頭文字D 新作", "name_cn": "头文字D 新作",
                  "date": None, "platform": "TV", "summary": "unknown season"}
    _install_bangumi_mock(monkeypatch, relations=relations)
    monkeypatch.setattr(
        br, "get_subject", lambda client, sid: _async_detail(details, sid)
    )
    monkeypatch.setattr(
        mb, "get_subject", lambda client, sid: _async_detail(details, sid)
    )
    collection, seed = await _make_seed_work(db_session)
    resource = await _make_resource(
        db_session, is_batch=True, batch_scope="multi_season", series_id=seed.id,
    )

    await br.expand_bangumi_series_graph(db_session, resource)
    await db_session.commit()

    series, _ = await _works_by_bangumi_id(db_session)
    assert "bangumi:2" in series  # marked sequel expanded
    assert "bangumi:9" not in series  # indeterminate season skipped


async def _async_detail(details, sid):
    return details.get(int(sid), {})


async def test_second_specials_subject_gets_own_shell(db_session, monkeypatch):
    """Two 番外篇 subjects cannot share the (collection, season 0) slot (F4):
    the earliest-aired one takes it; the second becomes its own season-1
    shell work and is still linked (cross-collection links are legal)."""
    relations = {
        1: [
            {"id": 4, "type": 2, "relation": "番外篇", "name": "頭文字D Extra Stage",
             "name_cn": "头文字D Extra Stage", "date": "2000-02-21"},
            {"id": 10, "type": 2, "relation": "番外篇", "name": "頭文字D Battle Stage",
             "name_cn": "头文字D Battle Stage", "date": "2002-05-15"},
        ],
    }
    details = dict(_DETAILS)
    details[10] = {"id": 10, "name": "頭文字D Battle Stage",
                   "name_cn": "头文字D Battle Stage", "date": "2002-05-15",
                   "platform": "OVA", "summary": "battle"}
    _install_bangumi_mock(monkeypatch, relations=relations)
    monkeypatch.setattr(
        br, "get_subject", lambda client, sid: _async_detail(details, sid)
    )
    monkeypatch.setattr(
        mb, "get_subject", lambda client, sid: _async_detail(details, sid)
    )
    collection, seed = await _make_seed_work(db_session)
    resource = await _make_resource(
        db_session, is_batch=True, batch_scope="multi_season", series_id=seed.id,
    )

    await br.expand_bangumi_series_graph(db_session, resource)
    await db_session.commit()

    series, _ = await _works_by_bangumi_id(db_session)
    # The earliest specials subject takes the season-0 slot…
    assert series["bangumi:4"].season_number == 0
    assert series["bangumi:4"].collection_id == collection.id
    # …the second is never merged into it: it becomes its own season-1 shell
    # work in a separate auto collection, still linked to the resource.
    second = series["bangumi:10"]
    assert second.season_number == 1
    assert second.collection_id != collection.id
    shell = await db_session.get(WorkCollection, second.collection_id)
    assert shell is not None and shell.external_source == "series_group"
    season_zero = [w for w in series.values() if w.season_number == 0]
    assert len(season_zero) == 1
    links = await _links(db_session, resource.id)
    assert {link.series_id for link in links} >= {seed.id, series["bangumi:4"].id, second.id}
    # Cross-collection span is reported faithfully.
    await db_session.refresh(resource)
    assert resource.collection_id is None


# ---------------------------------------------------------------------------
# F1/F2 — same-IP gate & scoped air-date inference
# ---------------------------------------------------------------------------


async def test_cross_ip_sequel_rejected_and_not_traversed(db_session, monkeypatch):
    """F1: a 续集 edge into a different IP (頭文字D Final Stage → MFゴースト)
    fails the same-IP gate — never upserted, never linked, and BFS does not
    continue into the foreign sub-graph."""
    fetched: list[int] = []

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

    monkeypatch.setattr(br, "bangumi_configured", lambda: True)
    monkeypatch.setattr(br, "get_subject_relations", _relations)
    monkeypatch.setattr(br, "get_subject", lambda client, sid: _async_detail(details, sid))
    monkeypatch.setattr(mb, "get_subject", lambda client, sid: _async_detail(details, sid))
    monkeypatch.setattr(mb, "get_subject_episodes", _episodes_stub)
    collection, seed = await _make_seed_work(db_session)
    resource = await _make_resource(
        db_session, is_batch=True, batch_scope="multi_season", series_id=seed.id,
    )

    await br.expand_bangumi_series_graph(db_session, resource)
    await db_session.commit()

    series, _ = await _works_by_bangumi_id(db_session)
    # The unmarked in-IP sequel (Final Stage) is expanded via air-date order…
    assert series["bangumi:8"].season_number == 2
    assert series["bangumi:8"].collection_id == collection.id
    # …the foreign IP is never upserted and never traversed.
    assert "bangumi:20" not in series
    assert "bangumi:21" not in series
    assert 20 not in fetched
    links = await _links(db_session, resource.id)
    assert {link.series_id for link in links} == {seed.id, series["bangumi:8"].id}


async def _episodes_stub(client, sid):
    return [{"sort": 1, "ep": 1, "name": "ep1", "airdate": "2000-01-01"}]


async def test_airdate_threshold_ignores_rejected_foreign_nodes(db_session, monkeypatch):
    """F2: the air-date threshold is computed within the gated set only — a
    foreign node's later date (MFゴースト 3rd Season, 2026) must not push the
    threshold past a legitimate unmarked sequel (Final Stage, 2014)."""
    relations = {
        1: [
            {"id": 6, "type": 2, "relation": "续集", "name": "頭文字D Fifth Stage",
             "name_cn": "头文字D Fifth Stage", "date": "2012-11-04"},
            {"id": 20, "type": 2, "relation": "续集", "name": "MFゴースト 3rd Season",
             "name_cn": "极速车魂 第三季", "date": "2026-01-04"},
        ],
        6: [
            {"id": 8, "type": 2, "relation": "续集", "name": "頭文字D Final Stage",
             "name_cn": "头文字D Final Stage", "date": "2014-05-16"},
        ],
    }
    details = dict(_DETAILS)
    details[20] = {"id": 20, "name": "MFゴースト 3rd Season", "name_cn": "极速车魂 第三季",
                   "date": "2026-01-04", "platform": "TV", "summary": "mfg3"}
    _install_bangumi_mock(monkeypatch, relations=relations)
    monkeypatch.setattr(br, "get_subject", lambda client, sid: _async_detail(details, sid))
    monkeypatch.setattr(mb, "get_subject", lambda client, sid: _async_detail(details, sid))
    collection, seed = await _make_seed_work(db_session)
    resource = await _make_resource(
        db_session, is_batch=True, batch_scope="multi_season", series_id=seed.id,
    )

    await br.expand_bangumi_series_graph(db_session, resource)
    await db_session.commit()

    series, _ = await _works_by_bangumi_id(db_session)
    assert series["bangumi:6"].season_number == 5
    # Final Stage (2014) follows Fifth Stage (2012) — not skipped because the
    # rejected 2026 foreign node never enters the threshold computation.
    assert series["bangumi:8"].season_number == 6
    assert "bangumi:20" not in series


# ---------------------------------------------------------------------------
# F3b — same-name auto collection absorption
# ---------------------------------------------------------------------------


async def test_same_name_auto_collection_absorbed_into_resource_collection(
    db_session, monkeypatch
):
    """F3b: the per-season upsert's series_group collection and the pack's
    franchise_pack collection are duplicates of one IP — the graph absorbs
    the multi-member auto collection (same normalized base name) whole."""
    _install_bangumi_mock(monkeypatch)
    # The series_group collection the per-season upserts converged on, with
    # TWO members so the single-member shell absorber refuses it.
    shell = WorkCollection(
        id=_uuid(), title_cn="头文字D",
        external_id=None, external_source="series_group",
    )
    first = TVSeries(
        id=_uuid(), title_cn="头文字D", original_title="頭文字D",
        external_source="bangumi", external_id="bangumi:1",
        content_type="tv", season_number=1, start_date=date(1998, 4, 18),
        collection_id=shell.id,
    )
    second = TVSeries(
        id=_uuid(), title_cn="头文字D Second Stage", original_title="頭文字D Second Stage",
        external_source="bangumi", external_id="bangumi:2",
        content_type="tv", season_number=2, start_date=date(1999, 10, 15),
        collection_id=shell.id,
    )
    # The pack's own franchise collection (cleaned pack title).
    pack = WorkCollection(
        id=_uuid(), title_cn="头文字D Initial D",
        external_id=None, external_source="franchise_pack",
    )
    db_session.add_all([shell, first, second, pack])
    await db_session.commit()
    resource = await _make_resource(
        db_session, is_batch=True, batch_scope="franchise", collection_id=pack.id,
    )
    db_session.add(ResourceWorkLink(
        id=_uuid(), resource_id=resource.id, series_id=first.id, source="auto",
    ))
    await db_session.commit()

    await br.expand_bangumi_series_graph(db_session, resource)
    await db_session.commit()

    # The duplicate series_group collection is gone; both season works moved.
    assert await db_session.get(WorkCollection, shell.id) is None
    await db_session.refresh(first)
    await db_session.refresh(second)
    assert first.collection_id == pack.id
    assert second.collection_id == pack.id
    # The graph linked the newly attached member.
    links = await _links(db_session, resource.id)
    assert {link.series_id for link in links} >= {first.id, second.id}
    # The resource keeps its franchise collection.
    await db_session.refresh(resource)
    assert resource.collection_id == pack.id


async def test_manually_edited_same_name_collection_is_not_absorbed(
    db_session, monkeypatch
):
    """F3b: a same-name auto collection carrying manual edits is never
    absorbed — the work stays put and is not linked."""
    _install_bangumi_mock(monkeypatch)
    shell = WorkCollection(
        id=_uuid(), title_cn="头文字D",
        external_id=None, external_source="series_group",
        manually_edited_fields=["title_cn"],
    )
    first = TVSeries(
        id=_uuid(), title_cn="头文字D", original_title="頭文字D",
        external_source="bangumi", external_id="bangumi:1",
        content_type="tv", season_number=1, start_date=date(1998, 4, 18),
        collection_id=shell.id,
    )
    second = TVSeries(
        id=_uuid(), title_cn="头文字D Second Stage", original_title="頭文字D Second Stage",
        external_source="bangumi", external_id="bangumi:2",
        content_type="tv", season_number=2, start_date=date(1999, 10, 15),
        collection_id=shell.id,
    )
    pack = WorkCollection(
        id=_uuid(), title_cn="头文字D Initial D",
        external_id=None, external_source="franchise_pack",
    )
    db_session.add_all([shell, first, second, pack])
    await db_session.commit()
    resource = await _make_resource(
        db_session, is_batch=True, batch_scope="franchise", collection_id=pack.id,
    )
    db_session.add(ResourceWorkLink(
        id=_uuid(), resource_id=resource.id, series_id=first.id, source="auto",
    ))
    await db_session.commit()

    await br.expand_bangumi_series_graph(db_session, resource)
    await db_session.commit()

    assert await db_session.get(WorkCollection, shell.id) is not None
    await db_session.refresh(second)
    assert second.collection_id == shell.id
    links = await _links(db_session, resource.id)
    assert second.id not in {link.series_id for link in links}


# ---------------------------------------------------------------------------
# C1 — two-phase season-slot admission
# ---------------------------------------------------------------------------


async def test_correction_frees_slot_for_later_node(db_session, monkeypatch):
    """C1: a hint-parked work holding the WRONG season slot is corrected
    first (phase A), so the rightful owner admitted later (phase B) still
    lands — the "Fifth parked by Final, then Final corrected" sequence."""
    relations = {
        1: [
            {"id": 6, "type": 2, "relation": "续集", "name": "頭文字D Fifth Stage",
             "name_cn": "头文字D Fifth Stage", "date": "2012-11-04"},
        ],
        6: [
            {"id": 8, "type": 2, "relation": "续集", "name": "頭文字D Final Stage",
             "name_cn": "头文字D Final Stage", "date": "2014-05-16"},
        ],
    }
    _install_bangumi_mock(monkeypatch, relations=relations)
    collection, seed = await _make_seed_work(db_session)
    # The misplaced work: Final Stage parked at season 5 by a cluster hint
    # (pack-internal numbering), occupying Fifth Stage's slot.
    misplaced = TVSeries(
        id=_uuid(), title_cn="头文字D Final Stage",
        original_title="頭文字D Final Stage",
        external_source="bangumi", external_id="bangumi:8",
        content_type="tv", season_number=5, start_date=date(2014, 5, 16),
        collection_id=collection.id,
    )
    db_session.add(misplaced)
    db_session.add(WorkExternalId(
        id=_uuid(), work_type="series", work_id=misplaced.id,
        source="bangumi", external_id="bangumi:8",
    ))
    await db_session.commit()
    resource = await _make_resource(
        db_session, is_batch=True, batch_scope="multi_season",
        series_id=seed.id, batch_seasons=[1],
    )

    created = await br.expand_bangumi_series_graph(db_session, resource)
    await db_session.commit()

    series, _ = await _works_by_bangumi_id(db_session)
    # The hint-parked work was corrected to its true season first…
    assert series["bangumi:8"].season_number == 6
    assert series["bangumi:8"].collection_id == collection.id
    # …freeing s5 for Fifth Stage, admitted in the same pass.
    assert series["bangumi:6"].season_number == 5
    assert series["bangumi:6"].collection_id == collection.id
    links = await _links(db_session, resource.id)
    assert {link.series_id for link in links} >= {
        seed.id, series["bangumi:6"].id, series["bangumi:8"].id,
    }
    assert created == 3  # both nodes newly linked + the seed itself
