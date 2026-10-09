"""Actual metadata merge must retain candidates and human decision history."""

import pytest
from sqlalchemy import select

from app.models.agent import Agent
from app.models.movie import Movie
from app.models.pending_decision import PendingDecision
from app.services.agent_service import create_pending_decision
from app.services.metadata_dedup import DedupReport, _merge_movie_group
from tests.unit.test_agent_service import _make_resource
from tests.unit.test_agent_service import channel as channel
from tests.unit.test_agent_service import downloader as downloader


@pytest.mark.parametrize("rollback", [False, True])
async def test_movie_merge_rekeys_and_unions_choices_without_deleting_history(
    db_session, channel, downloader, rollback
):
    movies = [
        Movie(title_cn=f"Synthetic duplicate {i}", external_source="tmdb", external_id="tmdb:123456") for i in range(2)
    ]
    agent = Agent(name="Synthetic agent", channel_id=channel.id, downloader_id=downloader.id, scope_channel_wide=True)
    db_session.add_all([*movies, agent])
    await db_session.flush()
    expected = set()
    history_ids = []
    for movie in movies:
        candidates = [_make_resource(channel.id, movie_id=movie.id, season=None, episode=None) for _ in range(2)]
        db_session.add_all(candidates)
        await db_session.flush()
        expected.update(r.id for r in candidates)
        await create_pending_decision(agent, ("movie", movie.id, None), candidates, db_session, skip_llm=True)
        history = PendingDecision(
            agent_id=agent.id,
            movie_id=movie.id,
            candidates=[r.id for r in candidates],
            reason="Original human evidence",
            status="decided",
            decided_resource_id=candidates[0].id,
        )
        db_session.add(history)
        await db_session.flush()
        history_ids.append(history.id)
    if rollback:
        with pytest.raises(RuntimeError, match="after rekey"):
            async with db_session.begin_nested():
                await _merge_movie_group(db_session, movies, DedupReport(), survivor=movies[0])
                await db_session.flush()
                raise RuntimeError("after rekey")
        rows = list(await db_session.scalars(select(PendingDecision)))
        assert len(rows) == 4 and sum(row.status == "pending" for row in rows) == 2
        assert set().union(*(set(row.candidates) for row in rows if row.status == "pending")) == expected
        from app.models.decision_migration import DecisionMigration

        assert not list(await db_session.scalars(select(DecisionMigration)))
        return
    await _merge_movie_group(db_session, movies, DedupReport(), survivor=movies[0])
    await db_session.flush()
    pending = list(await db_session.scalars(select(PendingDecision).where(PendingDecision.status == "pending")))
    assert len(pending) == 1 and set(pending[0].candidates) == expected
    assert pending[0].decision_scope["work_id"] == movies[0].id
    histories = list(await db_session.scalars(select(PendingDecision).where(PendingDecision.id.in_(history_ids))))
    assert len(histories) == 2
    assert all(
        row.status == "decided" and row.reason == "Original human evidence" and row.decided_resource_id
        for row in histories
    )

    from app.models.decision_migration import DecisionMigration
    from app.services.decision_rekey import rekey_agent_choices

    stable_id = pending[0].id
    await rekey_agent_choices(db_session, [agent.id])
    again = list(await db_session.scalars(select(PendingDecision).where(PendingDecision.status == "pending")))
    assert len(again) == 1 and again[0].id == stable_id
    archives = list(await db_session.scalars(select(DecisionMigration)))
    assert len(archives) == 1 and archives[0].result["operation"] == "work_rekey"


async def test_links_only_series_merge_discovers_and_rekeys_affected_decisions(db_session, channel, downloader):
    from app.models.resource_file_assignment import ResourceFileAssignment
    from app.models.resource_work_link import ResourceWorkLink
    from app.models.series import TVSeries
    from app.models.work_collection import WorkCollection
    from app.services.agent_service import _batch_coverage_key
    from app.services.metadata_dedup import _merge_series_group
    from app.services.resource_coverage import load_batch_coverage

    collections = [WorkCollection(title_cn=f"Synthetic collection {i}") for i in range(2)]
    db_session.add_all(collections)
    await db_session.flush()
    survivor = TVSeries(title_cn="Synthetic S1", season_number=1, collection_id=collections[0].id)
    duplicate = TVSeries(title_cn="Synthetic S1 duplicate", season_number=1, collection_id=collections[1].id)
    second = TVSeries(title_cn="Synthetic S2", season_number=2, collection_id=collections[0].id)
    agent = Agent(
        name="Synthetic multi agent", channel_id=channel.id, downloader_id=downloader.id, scope_channel_wide=True
    )
    db_session.add_all([survivor, duplicate, second, agent])
    await db_session.flush()
    expected = set()
    duplicate_id = duplicate.id
    for first in [survivor, duplicate]:
        candidates = []
        for _ in range(2):
            resource = _make_resource(
                channel.id,
                series_id=None,
                movie_id=None,
                season=None,
                episode=None,
                is_batch=True,
                batch_scope="multi_season",
                batch_seasons=[1, 2],
            )
            resource.work_links = [ResourceWorkLink(series_id=w.id) for w in [first, second]]
            resource.file_assignments = [
                ResourceFileAssignment(
                    series_id=w.id,
                    season=w.season_number,
                    file_path=f"season-{w.season_number}.mkv",
                    episode_start=1,
                    episode_end=12,
                )
                for w in [first, second]
            ]
            db_session.add(resource)
            candidates.append(resource)
        await db_session.flush()
        await load_batch_coverage(db_session, candidates)
        expected.update(r.id for r in candidates)
        row = await create_pending_decision(
            agent,
            ("series", None, None, -1),
            candidates,
            db_session,
            coverage=_batch_coverage_key(candidates[0]),
            skip_llm=True,
        )
        assert row.series_id is None and row.movie_id is None
    await _merge_series_group(db_session, [survivor, duplicate], DedupReport(), survivor=survivor)
    await db_session.flush()
    rows = list(await db_session.scalars(select(PendingDecision).where(PendingDecision.status == "pending")))
    assert len(rows) == 1 and set(rows[0].candidates) == expected
    descriptors = rows[0].decision_scope["coverage"][1]
    assert {d[1] for d in descriptors} == {survivor.id, second.id}
    assert duplicate_id not in {d[1] for d in descriptors}


@pytest.mark.parametrize("entry", ["rehome", "cross_type"])
async def test_series_to_movie_rekeys_and_combines_target_candidates(db_session, channel, downloader, entry):
    from app.models.series import TVSeries
    from app.models.work_collection import WorkCollection
    from app.services.metadata_dedup import merge_cross_type_duplicates, rehome_series_as_movie

    collection = WorkCollection(title_cn="Synthetic misfiled collection")
    db_session.add(collection)
    await db_session.flush()
    series = TVSeries(
        title_cn="Synthetic same entity",
        season_number=1,
        collection_id=collection.id,
        content_type="movie",
        external_source="tmdb",
        external_id="tmdb:234567",
    )
    movie = Movie(title_cn="Synthetic same entity", external_source="tmdb", external_id="tmdb:234567")
    agent = Agent(
        name="Synthetic cross-type agent", channel_id=channel.id, downloader_id=downloader.id, scope_channel_wide=True
    )
    db_session.add_all([series, movie, agent])
    await db_session.flush()
    expected = set()
    for kind, work in [("series", series), ("movie", movie)]:
        resources = [
            _make_resource(
                channel.id,
                series_id=work.id if kind == "series" else None,
                movie_id=work.id if kind == "movie" else None,
                season=None,
                episode=None,
            )
            for _ in range(2)
        ]
        db_session.add_all(resources)
        await db_session.flush()
        expected.update(r.id for r in resources)
        if kind == "series":
            # Historical misclassified movie: preserve it for rehome testing,
            # but new production choices must reject its unknown TV episode.
            with pytest.raises(ValueError, match="changed identity"):
                await create_pending_decision(agent, (kind, work.id, None), resources, db_session, skip_llm=True)
            from app.services.decision_store import choice_identity

            key, scope = choice_identity("series", work.id, 1, None)
            db_session.add(PendingDecision(
                agent_id=agent.id, series_id=work.id, season=1, episode=None,
                decision_key=key, decision_scope=scope, candidates=[r.id for r in resources],
                status="pending", reason="Historical misclassified movie",
            ))
            await db_session.flush()
        else:
            await create_pending_decision(agent, (kind, work.id, None), resources, db_session, skip_llm=True)
    if entry == "rehome":
        await rehome_series_as_movie(db_session, series, movie)
    else:
        report = await merge_cross_type_duplicates(db_session)
        assert report.cross_type_merges == 1
    await db_session.flush()
    rows = list(await db_session.scalars(select(PendingDecision).where(PendingDecision.status == "pending")))
    assert len(rows) == 1 and set(rows[0].candidates) == expected
    assert rows[0].series_id is None and rows[0].movie_id == movie.id
    assert rows[0].decision_scope["kind"] == "movie" and rows[0].decision_scope["work_id"] == movie.id


@pytest.mark.parametrize("movie_has_season", [True, False])
async def test_movie_to_series_rekeys_only_candidates_with_explicit_season(
    db_session, channel, downloader, movie_has_season
):
    """Synthetic misclassification: a title match must not invent a season."""
    from app.models.decision_migration import DecisionMigration
    from app.models.series import TVSeries
    from app.models.work_collection import WorkCollection
    from app.services.metadata_dedup import merge_cross_type_duplicates

    collection = WorkCollection(title_cn="Synthetic reverse conversion")
    db_session.add(collection)
    await db_session.flush()
    series = TVSeries(
        title_cn="Synthetic episode work", collection_id=collection.id, season_number=2,
        external_source="tmdb", external_id="tmdb:9002",
    )
    movie = Movie(title_cn="Synthetic episode work", external_source="tmdb", external_id="tmdb:9002")
    agent = Agent(
        name="Synthetic reverse agent", channel_id=channel.id, downloader_id=downloader.id,
        scope_channel_wide=True,
    )
    db_session.add_all([series, movie, agent])
    await db_session.flush()
    expected = set()
    unknown = set()
    for kind, work in [("series", series), ("movie", movie)]:
        season = 2 if kind == "series" or movie_has_season else None
        resources = [
            _make_resource(
                channel.id, series_id=work.id if kind == "series" else None,
                movie_id=work.id if kind == "movie" else None,
                season=season, episode=3, episode_confidence="manual",
            )
            for _ in range(2)
        ]
        db_session.add_all(resources)
        await db_session.flush()
        (expected if season is not None else unknown).update(r.id for r in resources)
        key = (kind, work.id, 2, 3) if kind == "series" else (kind, work.id, None)
        await create_pending_decision(agent, key, resources, db_session, skip_llm=True)
    report = await merge_cross_type_duplicates(db_session)
    assert report.cross_type_merges == 1
    await db_session.flush()
    rows = list(await db_session.scalars(select(PendingDecision).where(PendingDecision.status == "pending")))
    assert len(rows) == 1 and set(rows[0].candidates) == expected
    assert rows[0].movie_id is None and rows[0].series_id == series.id
    assert rows[0].decision_scope["season"] == 2
    assert rows[0].decision_scope["episode"] == 3
    if unknown:
        archives = list(await db_session.scalars(select(DecisionMigration)))
        assert len(archives) == 1
        assert {item["resource_id"] for item in archives[0].original_review["blocked"]} == unknown


@pytest.mark.parametrize("rollback", [False, True])
async def test_season_split_rekeys_mixed_legacy_candidates(db_session, channel, downloader, rollback):
    from app.models.decision_migration import DecisionMigration
    from scripts.season_split_migration import migrate_series
    from tests.unit.test_season_split_migration import _legacy_multi_season_series

    series = await _legacy_multi_season_series(db_session)
    agent = Agent(name="Synthetic split agent", channel_id=channel.id, downloader_id=downloader.id)
    db_session.add(agent)
    await db_session.flush()
    by_season = {}
    for season in (1, 2):
        resources = [_make_resource(channel.id, series_id=series.id, season=season, episode=1,
                                    episode_confidence="manual") for _ in range(2)]
        db_session.add_all(resources)
        await db_session.flush()
        by_season[season] = {r.id for r in resources}
    # A legacy slot can contain candidates from different seasons. Preserve it
    # as source evidence, then derive replacement slots from routed resources.
    from app.services.decision_store import choice_identity
    key, scope = choice_identity("series", series.id, 1, 1)
    old = PendingDecision(agent_id=agent.id, series_id=series.id, season=1, episode=1,
                          candidates=sorted(set.union(*by_season.values())),
                          decision_key=key, decision_scope=scope, status="pending", reason="Synthetic legacy mixed slot")
    db_session.add(old)
    await db_session.flush()
    old_id, series_id = old.id, series.id
    if rollback:
        with pytest.raises(RuntimeError, match="split rollback"):
            async with db_session.begin_nested():
                await migrate_series(db_session, series, apply=True)
                raise RuntimeError("split rollback")
        restored = await db_session.get(PendingDecision, old_id, populate_existing=True)
        assert restored.status == "pending" and restored.decision_key == key
        assert set(restored.candidates) == set.union(*by_season.values())
        assert not list(await db_session.scalars(select(DecisionMigration)))
        return
    await migrate_series(db_session, series, apply=True)
    rows = list(await db_session.scalars(select(PendingDecision).where(PendingDecision.status == "pending")))
    assert len(rows) == 2
    for row in rows:
        assert set(row.candidates) == by_season[row.season]
        assert row.decision_scope["work_id"] == row.series_id
        assert row.decision_scope["season"] == row.season
        assert (row.series_id == series_id) == (row.season == 1)
    assert (await db_session.get(PendingDecision, old_id)).status == "expired"
    assert len(list(await db_session.scalars(select(DecisionMigration)))) == 1
