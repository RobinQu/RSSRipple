"""Unit tests for the resource association service (edit-wizard write path).

Covers branches of ``apply_association_update`` and its helpers that the API
integration suite does not exercise: external-candidate materialization,
defensive guards, the media-file completeness listing, non-batch single
episode-field application and stale-row cleanup.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest

from app.models.file_resource import FileResource
from app.models.movie import Movie
from app.models.resource_file_assignment import ResourceFileAssignment
from app.models.series import TVSeries
from app.models.work_collection import WorkCollection
from app.schemas.file_resource import (
    AssociationFileAssignment,
    AssociationWorkRef,
    ResourceAssociationUpdateRequest,
)
from app.schemas.metadata_search import MetadataCandidate
from app.services.resource_association import (
    AssociationValidationError,
    apply_association_update,
)


def _uuid() -> str:
    return str(uuid.uuid4())


def _series(title: str = "攻壳机动队", **kw) -> TVSeries:
    return TVSeries(
        id=_uuid(), title_cn=title, title_en=title,
        original_title=title, content_type="tv", **kw,
    )


def _movie(title: str = "哈姆奈特") -> Movie:
    return Movie(
        id=_uuid(), title_cn=title, title_en=title,
        original_title=title, content_type="movie",
    )


async def _seed_series(db_session, title: str = "攻壳机动队", **kw) -> TVSeries:
    series = _series(title, **kw)
    db_session.add(series)
    await db_session.flush()
    return series


async def _seed_movie(db_session, title: str = "哈姆奈特") -> Movie:
    movie = _movie(title)
    db_session.add(movie)
    await db_session.flush()
    return movie


async def _seed_collection(db_session, title: str = "合集") -> WorkCollection:
    collection = WorkCollection(
        id=_uuid(), title_cn=title, external_source="series_group",
        external_id=_uuid(),
    )
    db_session.add(collection)
    await db_session.flush()
    return collection


async def _seed_resource(db_session, *, series_id=None, movie_id=None,
                         **kw) -> FileResource:
    from app.models.channel import Channel

    channel = Channel(
        id=kw.pop("channel_id", _uuid()), name="ch", type="rss_feed",
        url="https://example.com/rss",
        field_mapping={"list_locator": {"source": "entries"},
                       "field_mappings": {"torrent_url": {"source": "link"}}},
        metadata_agent_enabled=False,
    )
    db_session.add(channel)
    await db_session.flush()
    resource = FileResource(
        id=_uuid(), channel_id=channel.id,
        guid=_uuid(), title_raw=kw.pop("title_raw", "raw"),
        torrent_url="magnet:?xt=urn:btih:abc",
        series_id=series_id, movie_id=movie_id,
        parsed_at=datetime.now(UTC), **kw,
    )
    db_session.add(resource)
    await db_session.flush()
    return resource


def _ref(work) -> AssociationWorkRef:
    wt = "series" if isinstance(work, TVSeries) else "movie"
    return AssociationWorkRef(work_type=wt, work_id=work.id)


def _asg(path: str, work, **kw) -> AssociationFileAssignment:
    wt = "series" if isinstance(work, TVSeries) else "movie"
    return AssociationFileAssignment(
        file_path=path, work_type=wt, work_id=work.id, **kw
    )


def _external_candidate(content_type: str = "tv", **kw) -> MetadataCandidate:
    return MetadataCandidate(
        origin="external",
        content_type=content_type,  # type: ignore[arg-type]
        title_cn=kw.pop("title_cn", "候选作品"),
        title_en=kw.pop("title_en", "Candidate"),
        primary_source=kw.pop("primary_source", "wikipedia"),
        identity_source=kw.pop("identity_source", "wikipedia"),
        external_id=kw.pop("external_id", "w:123"),
        match_path=kw.pop("match_path", "primary"),
        selectable=True,
        metadata=kw.pop("metadata", {}),
        **kw,
    )


# ---------------------------------------------------------------------------
# External-candidate materialization (_resolve_works candidate branches)
# ---------------------------------------------------------------------------


async def test_candidate_series_materializes_and_maps_assignments(db_session):
    """A selectable external candidate upserts a series work; the resolved id
    is written back onto the ref and into matching assignments."""
    series = _series()
    db_session.add(series)
    await db_session.flush()

    candidate = _external_candidate(
        "tv", title_cn=series.title_cn, title_en=series.title_en,
    )
    ref = AssociationWorkRef(
        work_type="series", candidate=candidate, client_key="ck1",
    )
    asg = AssociationFileAssignment(
        file_path="e01.mkv", work_type="series", work_id="ck1",
        season=1, episode_start=1, episode_end=1,
    )
    resource = await _seed_resource(db_session)

    async def _fake_upsert(db, data):
        return series

    with patch(
        "app.services.metadata_service.create_or_update_series_from_external",
        new=AsyncMock(side_effect=_fake_upsert),
    ):
        body = ResourceAssociationUpdateRequest(
            is_batch=True, works=[ref], assignments=[asg],
        )
        await apply_association_update(db_session, resource, body)
        await db_session.flush()

    assert ref.work_id == series.id
    assert asg.work_id == series.id
    assert resource.series_id == series.id


async def test_candidate_series_none_season_raises(db_session):
    candidate = _external_candidate("tv")
    ref = AssociationWorkRef(
        work_type="series", candidate=candidate, client_key="ck1",
    )
    resource = await _seed_resource(db_session)

    with patch(
        "app.services.metadata_service.create_or_update_series_from_external",
        new=AsyncMock(return_value=None),
    ):
        body = ResourceAssociationUpdateRequest(
            is_batch=True, works=[ref],
        )
        with pytest.raises(AssociationValidationError, match="季号"):
            await apply_association_update(db_session, resource, body)


async def test_candidate_movie_materializes(db_session):
    movie = _movie()
    db_session.add(movie)
    await db_session.flush()
    candidate = _external_candidate("movie", title_cn=movie.title_cn)
    ref = AssociationWorkRef(
        work_type="movie", candidate=candidate, client_key="ck1",
    )
    resource = await _seed_resource(db_session)

    async def _fake_upsert(db, data):
        return movie

    with patch(
        "app.services.metadata_service.create_or_update_movie_from_external",
        new=AsyncMock(side_effect=_fake_upsert),
    ):
        body = ResourceAssociationUpdateRequest(
            is_batch=True, works=[ref],
        )
        await apply_association_update(db_session, resource, body)
        await db_session.flush()

    assert ref.work_id == movie.id
    assert resource.movie_id == movie.id
    assert resource.batch_scope == "movies"


async def test_work_ref_missing_work_id_raises(db_session):
    """Defensive guard: a ref with neither candidate nor work_id."""
    ref = AssociationWorkRef(work_type="series", work_id=_uuid())
    resource = await _seed_resource(db_session)
    body = ResourceAssociationUpdateRequest(is_batch=False, works=[ref])
    with pytest.raises(AssociationValidationError, match="作品不存在"):
        await apply_association_update(db_session, resource, body)


async def test_transient_resource_refresh_failure_is_tolerated(db_session):
    transient = FileResource(
        id=_uuid(), channel_id=_uuid(), guid=_uuid(),
        title_raw="raw", torrent_url="magnet:?xt=urn:btih:x",
    )
    body = ResourceAssociationUpdateRequest(is_batch=False, works=[])
    result = await apply_association_update(db_session, transient, body)
    assert result.warnings == []


# ---------------------------------------------------------------------------
# Batch scope derivation edge branches
# ---------------------------------------------------------------------------


async def test_batch_no_works_falls_back_to_franchise(db_session):
    resource = await _seed_resource(db_session)
    body = ResourceAssociationUpdateRequest(is_batch=True, works=[], assignments=[])
    await apply_association_update(db_session, resource, body)
    await db_session.flush()
    assert resource.batch_scope == "franchise"


async def test_batch_series_across_collections_derives_franchise(db_session):
    c1 = await _seed_collection(db_session, "A")
    c2 = await _seed_collection(db_session, "B")
    s1 = await _seed_series(db_session, "A", collection_id=c1.id)
    s2 = await _seed_series(db_session, "B", collection_id=c2.id)
    resource = await _seed_resource(db_session)
    body = ResourceAssociationUpdateRequest(
        is_batch=True,
        works=[_ref(s1), _ref(s2)],
        assignments=[
            _asg("a.mkv", s1, season=1, episode_start=1, episode_end=1),
            _asg("b.mkv", s2, season=1, episode_start=1, episode_end=1),
        ],
    )
    await apply_association_update(db_session, resource, body)
    await db_session.flush()
    assert resource.batch_scope == "franchise"


# ---------------------------------------------------------------------------
# Assignment validation / media-file completeness
# ---------------------------------------------------------------------------


async def test_multi_work_whole_range_miss_rejected(db_session):
    collection = await _seed_collection(db_session, "某系列")
    s1 = await _seed_series(
        db_session, "S1", season_number=1, number_of_episodes=2,
        collection_id=collection.id,
    )
    s2 = await _seed_series(
        db_session, "S2", season_number=2, number_of_episodes=2,
        collection_id=collection.id,
    )
    resource = await _seed_resource(db_session)
    body = ResourceAssociationUpdateRequest(
        is_batch=True,
        works=[_ref(s1), _ref(s2)],
        assignments=[
            _asg("s1.mkv", s1, season=1, episode_start=1, episode_end=1),
            # s2 有指派但区间完全未覆盖其应有集数（1-2）。
            _asg("s2.mkv", s2, season=2, episode_start=3, episode_end=3),
        ],
    )
    with pytest.raises(AssociationValidationError, match="完全未覆盖"):
        await apply_association_update(db_session, resource, body)


async def test_known_media_files_completeness_check(db_session, tmp_path):
    """When the torrent listing is known, every media file must be assigned."""
    import bencodepy

    collection = await _seed_collection(db_session, "多季")
    s1 = await _seed_series(
        db_session, "S1", season_number=1, number_of_episodes=1,
        collection_id=collection.id,
    )
    s2 = await _seed_series(
        db_session, "S2", season_number=2, number_of_episodes=1,
        collection_id=collection.id,
    )
    resource = await _seed_resource(db_session)

    torrent = tmp_path / "pack.torrent"
    _MB = 1024 * 1024
    torrent.write_bytes(bencodepy.encode({
        b"info": {
            b"name": b"Pack",
            b"files": [
                {b"length": 500 * _MB, b"path": [b"Show S01E01.mkv"]},
                {b"length": 600 * _MB, b"path": [b"Show S01E02.mkv"]},
            ],
            b"piece length": 16384,
            b"pieces": b"x" * 20,
        }
    }))
    resource.torrent_file = str(torrent)
    await db_session.flush()

    body = ResourceAssociationUpdateRequest(
        is_batch=True,
        works=[_ref(s1), _ref(s2)],
        assignments=[
            _asg("Show S01E01.mkv", s1, season=1, episode_start=1, episode_end=1),
            # Show S01E02.mkv 未指派 → 422
            _asg("Show S01E02.mkv", s2, season=2, episode_start=1, episode_end=1),
        ],
    )
    result = await apply_association_update(db_session, resource, body)
    await db_session.flush()
    assert result.warnings == []

    # Now drop the E02 assignment -> S2 gets a different assignment so the
    # "every work needs a placement" check passes, but its media file in the
    # known listing is uncovered -> hard error.
    body2 = ResourceAssociationUpdateRequest(
        is_batch=True,
        works=[_ref(s1), _ref(s2)],
        assignments=[
            _asg("Show S01E01.mkv", s1, season=1, episode_start=1, episode_end=1),
            _asg("Show S01E03.mkv", s2, season=2, episode_start=1, episode_end=1),
        ],
    )
    with pytest.raises(AssociationValidationError, match="未指派"):
        await apply_association_update(db_session, resource, body2)


async def test_known_media_files_missing_listing_warns(db_session):
    """No torrent listing available -> completeness check skipped with a
    warning instead of a hard error."""
    collection = await _seed_collection(db_session, "多季")
    s1 = await _seed_series(
        db_session, "S1", season_number=1, number_of_episodes=1,
        collection_id=collection.id,
    )
    s2 = await _seed_series(
        db_session, "S2", season_number=2, number_of_episodes=1,
        collection_id=collection.id,
    )
    resource = await _seed_resource(db_session)  # no torrent_file set

    body = ResourceAssociationUpdateRequest(
        is_batch=True,
        works=[_ref(s1), _ref(s2)],
        assignments=[
            _asg("S1.mkv", s1, season=1, episode_start=1, episode_end=1),
            _asg("S2.mkv", s2, season=2, episode_start=1, episode_end=1),
        ],
    )
    result = await apply_association_update(db_session, resource, body)
    await db_session.flush()
    assert any("无法获取 torrent 文件清单" in w for w in result.warnings)


async def test_known_media_files_stale_path_warns(db_session, tmp_path):
    """torrent_file points at a missing file -> treated as no listing."""
    collection = await _seed_collection(db_session, "多季")
    s1 = await _seed_series(
        db_session, "S1", season_number=1, number_of_episodes=1,
        collection_id=collection.id,
    )
    s2 = await _seed_series(
        db_session, "S2", season_number=2, number_of_episodes=1,
        collection_id=collection.id,
    )
    resource = await _seed_resource(db_session)
    resource.torrent_file = str(tmp_path / "gone.torrent")
    await db_session.flush()

    body = ResourceAssociationUpdateRequest(
        is_batch=True,
        works=[_ref(s1), _ref(s2)],
        assignments=[
            _asg("S1.mkv", s1, season=1, episode_start=1, episode_end=1),
            _asg("S2.mkv", s2, season=2, episode_start=1, episode_end=1),
        ],
    )
    result = await apply_association_update(db_session, resource, body)
    assert any("无法获取 torrent 文件清单" in w for w in result.warnings)


# ---------------------------------------------------------------------------
# Non-batch branch: single episode fields + assignment cleanup
# ---------------------------------------------------------------------------


async def test_non_batch_single_episode_fields_and_confidence(db_session):
    series = await _seed_series(db_session)
    resource = await _seed_resource(db_session, episode_confidence="ambiguous")
    old = ResourceFileAssignment(
        resource_id=resource.id, file_path="old.mkv",
        series_id=series.id, source="auto",
    )
    db_session.add(old)
    await db_session.flush()

    body = ResourceAssociationUpdateRequest(
        is_batch=False,
        works=[_ref(series)],
        assignments=[_asg("ep05.mkv", series, season=1,
                          episode_start=5, episode_end=5)],
        season=1,
        episode=5,
        absolute_episode=105,
    )
    result = await apply_association_update(db_session, resource, body)
    await db_session.flush()

    assert result.warnings == []
    assert resource.series_id == series.id
    assert resource.episode == 5
    assert resource.season == 1
    assert resource.absolute_episode == 105
    assert resource.episode_confidence == "manual"
    # Non-batch clears all placements, including the stale one.
    assert list(resource.file_assignments) == []
    assert list(resource.work_links) == []


async def test_non_batch_only_season_field_applied(db_session):
    """Only the season key sent -> applied without touching episode."""
    series = await _seed_series(db_session)
    resource = await _seed_resource(db_session)
    body = ResourceAssociationUpdateRequest(
        is_batch=False, works=[_ref(series)],
        season=2,
    )
    await apply_association_update(db_session, resource, body)
    await db_session.flush()
    assert resource.season == 2
    assert resource.episode is None
    assert resource.episode_confidence == "manual"


# ---------------------------------------------------------------------------
# Diff-preserving work-link cleanup
# ---------------------------------------------------------------------------


async def test_batch_work_links_stale_row_removed(db_session):
    """A link pointing at a work no longer in the association set is dropped."""
    from app.models.resource_work_link import ResourceWorkLink

    s1 = await _seed_series(db_session, "A")
    s2 = await _seed_series(db_session, "B")
    resource = await _seed_resource(db_session)
    db_session.add(ResourceWorkLink(
        resource_id=resource.id, series_id=s1.id, source="auto",
    ))
    await db_session.flush()
    await db_session.refresh(resource, ["work_links", "file_assignments"])

    body = ResourceAssociationUpdateRequest(
        is_batch=True,
        works=[_ref(s2)],
        assignments=[_asg("b.mkv", s2, season=1,
                          episode_start=1, episode_end=1)],
    )
    await apply_association_update(db_session, resource, body)
    await db_session.flush()
    await db_session.refresh(resource, ["work_links", "file_assignments"])

    links = list(resource.work_links)
    assert len(links) == 1
    assert links[0].series_id == s2.id
    assert links[0].source == "manual"


async def test_assignments_file_size_updated(db_session):
    """A changed placement with an explicit file_size stores it."""
    series = await _seed_series(db_session)
    resource = await _seed_resource(db_session)
    body = ResourceAssociationUpdateRequest(
        is_batch=True,
        works=[_ref(series)],
        assignments=[_asg("e01.mkv", series, season=1,
                          episode_start=1, episode_end=1, file_size=12345)],
    )
    await apply_association_update(db_session, resource, body)
    await db_session.flush()
    rows = {a.file_path: a for a in resource.file_assignments}
    assert rows["e01.mkv"].file_size == 12345


async def test_batch_assignments_stale_row_removed(db_session):
    """Batch save replaces the placement set: rows absent from the payload are
    removed regardless of provenance."""
    series = await _seed_series(db_session)
    resource = await _seed_resource(db_session)
    db_session.add(ResourceFileAssignment(
        resource_id=resource.id, file_path="stale.mkv",
        series_id=series.id, season=1, episode_start=9, episode_end=9,
        source="llm",
    ))
    await db_session.flush()
    await db_session.refresh(resource, ["work_links", "file_assignments"])

    body = ResourceAssociationUpdateRequest(
        is_batch=True,
        works=[_ref(series)],
        assignments=[_asg("e01.mkv", series, season=1,
                          episode_start=1, episode_end=1)],
    )
    await apply_association_update(db_session, resource, body)
    await db_session.flush()
    await db_session.refresh(resource, ["work_links", "file_assignments"])

    rows = {a.file_path: a for a in resource.file_assignments}
    assert set(rows) == {"e01.mkv"}


async def test_batch_marks_ambiguous_confidence_manual(db_session):
    series = await _seed_series(db_session)
    resource = await _seed_resource(db_session, episode_confidence="ambiguous")
    body = ResourceAssociationUpdateRequest(
        is_batch=True, works=[_ref(series)],
        assignments=[_asg("e01.mkv", series, season=1,
                          episode_start=1, episode_end=1)],
    )
    await apply_association_update(db_session, resource, body)
    await db_session.flush()
    assert resource.episode_confidence == "manual"
