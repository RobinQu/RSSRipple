"""Reviewed cleanup must preserve associations and genuine legacy evidence."""

import copy
import json

import pytest
from sqlalchemy import event, select, update

from app.models.episode import Episode
from app.models.file_resource import FileResource
from app.models.resource_file_assignment import ResourceFileAssignment
from app.models.resource_work_link import ResourceWorkLink
from app.models.series import TVSeries
from app.models.work_collection import WorkCollection
from app.models.work_external_id import WorkExternalId
from app.services.metadata_episode_reconcile import work_verified_season
from scripts.retired_season_fields import apply_review, export_reviews, review_work


async def seed(db, channel):
    parent = WorkCollection(title_cn="Synthetic review parent")
    db.add(parent)
    await db.flush()
    work = TVSeries(
        title_cn="Synthetic protected work",
        collection_id=parent.id,
        season_number=1,
        number_of_seasons=1,
        manually_edited_fields=["title_cn", "number_of_seasons"],
    )
    db.add(work)
    await db.flush()
    resource = FileResource(
        channel_id=channel.id,
        guid=work.id,
        title_raw="Synthetic S1",
        torrent_url="https://example.invalid/torrent",
        series_id=work.id,
        season=1,
    )
    episode = Episode(series_id=work.id, season=1, episode=1, title="Synthetic preserved episode")
    db.add_all([resource, episode])
    await db.flush()
    assignment = ResourceFileAssignment(
        resource_id=resource.id,
        series_id=work.id,
        file_path="synthetic.mkv",
        season=1,
        episode_start=1,
        episode_end=1,
        source="manual",
    )
    bag = WorkExternalId(work_type="series", work_id=work.id, source="tmdb", external_id="tmdb:999001#s1")
    db.add_all([assignment, bag, ResourceWorkLink(resource_id=resource.id, series_id=work.id, source="manual")])
    await db.commit()
    return work, episode, resource, assignment, bag


async def test_cleanup_preserves_rows_identity_and_is_idempotent(db_session, sample_channel):
    work, *_ = await seed(db_session, sample_channel)
    assert work_verified_season(work) == 1
    review = await review_work(db_session, work.id)
    assert review["blocked_reasons"] == []
    review["confirmed_season"] = 1
    before = copy.deepcopy(review["snapshot"])
    assert (await apply_review(db_session, review))["changed"] is True
    await db_session.commit()
    assert work.number_of_seasons is None
    assert work_verified_season(work) == 1
    assert work.manually_edited_fields == ["season_number", "title_cn"]
    after = (await review_work(db_session, work.id))["snapshot"]
    for key in before.keys() - {"work"}:
        assert after[key] == before[key]
    for key in before["work"].keys() - {"number_of_seasons", "manually_edited_fields", "updated_at"}:
        assert after["work"][key] == before["work"][key]
    assert (await apply_review(db_session, review))["changed"] is False


@pytest.mark.parametrize(
    "case",
    [
        "no-confirmation",
        "wrong-season",
        "multi-season",
        "episode",
        "resource",
        "assignment",
        "identity",
        "stale",
        "tampered",
    ],
)
async def test_invalid_review_never_clears_data(db_session, sample_channel, case):
    work, episode, resource, assignment, bag = await seed(db_session, sample_channel)
    review = await review_work(db_session, work.id)
    review["confirmed_season"] = 1
    if case == "no-confirmation":
        review.pop("confirmed_season")
    elif case == "wrong-season":
        review["confirmed_season"] = 2
    elif case == "multi-season":
        work.number_of_seasons = 2
        work.seasons = [{"season_number": 1}, {"season_number": 2}]
    elif case == "episode":
        episode.season = 2
    elif case == "resource":
        resource.season = 2
    elif case == "assignment":
        assignment.season = 2
    elif case == "identity":
        bag.external_id = "tmdb:999001#s2"
    elif case == "stale":
        work.title_cn = "New manual title"
    elif case == "tampered":
        review["snapshot"]["work"]["title_cn"] = "Tampered export"
    await db_session.commit()
    if case in {"multi-season", "episode", "resource", "assignment", "identity"}:
        review = await review_work(db_session, work.id)
        review["confirmed_season"] = 1
    before = await review_work(db_session, work.id)
    with pytest.raises(ValueError):
        await apply_review(db_session, review)
    after = await review_work(db_session, work.id)
    assert after == before


async def test_post_flush_failure_rolls_back_cleanup(db_session, sample_channel):
    work, *_ = await seed(db_session, sample_channel)
    identity = work.id
    review = await review_work(db_session, identity)
    review["confirmed_season"] = 1
    await db_session.rollback()
    with pytest.raises(RuntimeError, match="injected"):
        async with db_session.begin():
            await apply_review(db_session, review)
            raise RuntimeError("injected after cleanup flush")
    assert await review_work(db_session, identity) == {k: v for k, v in review.items() if k != "confirmed_season"}


async def test_export_is_read_only_and_does_not_hide_blocked_works(db_engine, db_session, sample_channel, tmp_path):
    work, *_ = await seed(db_session, sample_channel)
    work.number_of_seasons = 2
    work.seasons = [{"season_number": 1}, {"season_number": 2}]
    await db_session.commit()
    statements = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement.strip().upper())

    event.listen(db_engine.sync_engine, "before_cursor_execute", capture)
    output = tmp_path / "review.jsonl"
    try:
        assert await export_reviews(str(output)) == 1
    finally:
        event.remove(db_engine.sync_engine, "before_cursor_execute", capture)
    assert all(s.startswith(("SELECT", "PRAGMA")) for s in statements)
    review = json.loads(output.read_text())
    assert review["work_id"] == work.id and review["blocked_reasons"]
    assert "confirmed_season" not in review
    assert (await db_session.scalar(select(TVSeries.number_of_seasons).where(TVSeries.id == work.id))) == 2


@pytest.mark.parametrize("confirmed", [None, True, "1", -1])
async def test_confirmation_is_an_explicit_nonnegative_integer(db_session, sample_channel, confirmed):
    work, *_ = await seed(db_session, sample_channel)
    review = await review_work(db_session, work.id)
    review["confirmed_season"] = confirmed
    with pytest.raises(ValueError, match="Explicit confirmed_season"):
        await apply_review(db_session, review)
    assert work.number_of_seasons == 1


def test_manual_confirmation_never_overrides_true_legacy_evidence():
    work = TVSeries(season_number=1, number_of_seasons=2, manually_edited_fields=["season_number"])
    assert work_verified_season(work) is None
    work.number_of_seasons = None
    work.seasons = [{"season_number": 1}, {"season_number": 2}]
    assert work_verified_season(work) is None
    work.seasons = None
    assert work_verified_season(work) == 1


@pytest.mark.parametrize("season,protection", [(None, ["season_number"]), (1, "season_number")])
def test_malformed_manual_confirmation_never_guesses_s1(season, protection):
    work = TVSeries(season_number=season, manually_edited_fields=protection)
    assert work_verified_season(work) is None


async def test_cleanup_does_not_rewrite_legacy_derived_fields(db_session, sample_channel):
    work, *_ = await seed(db_session, sample_channel)
    await db_session.execute(update(TVSeries).where(TVSeries.id == work.id).values(title_cn="Legacy manual SQL title"))
    await db_session.commit()
    review = await review_work(db_session, work.id)
    review["confirmed_season"] = 1
    assert (await apply_review(db_session, review))["changed"] is True
    after = (await review_work(db_session, work.id))["snapshot"]
    assert after["work"]["search_text"] == review["snapshot"]["work"]["search_text"]
    assert after["work"]["title_cn"] == "Legacy manual SQL title"
    assert (await apply_review(db_session, review))["changed"] is False
