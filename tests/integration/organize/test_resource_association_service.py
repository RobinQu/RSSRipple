"""资源关联编辑服务（resource_association）进程内集成测试。

覆盖 ``apply_association_update`` 的全部不变量分支：非合集单作品 FK、
合集恰一作品 FK 镜像 / 多作品仅 links、batch_scope 自动推导
（movies/franchise/season/multi_season/无作品回退）、diff-preserving 的
文件映射替换（provenance 保留）、校验硬错误与断档软警告、通用媒体字段
显式键生效。
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

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
from app.services.resource_association import (
    AssociationValidationError,
    apply_association_update,
)


def _uuid() -> str:
    return str(uuid.uuid4())


def _series(title: str = "攻壳机动队") -> TVSeries:
    return TVSeries(
        id=_uuid(), title_cn=title, title_en=title,
        original_title=title, content_type="tv", is_anime=True,
        start_date=date(2026, 4, 1),
    )


def _movie(title: str = "哈姆奈特") -> Movie:
    return Movie(
        id=_uuid(), title_cn=title, title_en=title,
        original_title=title, content_type="movie",
        release_date=date(2025, 12, 1),
    )


async def _seed_series(db_session, title: str = "攻壳机动队") -> TVSeries:
    series = _series(title)
    db_session.add(series)
    await db_session.flush()
    return series


async def _seed_movie(db_session, title: str = "哈姆奈特") -> Movie:
    movie = _movie(title)
    db_session.add(movie)
    await db_session.flush()
    return movie


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


# ---------------------------------------------------------------------------
# 非合集分支
# ---------------------------------------------------------------------------


async def test_non_batch_single_series_replaces_state(db_session):
    series = await _seed_series(db_session)
    resource = await _seed_resource(
        db_session, series_id=None,
        episode_confidence="ambiguous",
        resolution="1080p",
    )
    # 预置一条旧 auto 映射与一条 link：非合集保存应全部清掉
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
        fields={"resolution": "2160p", "not_a_media_field": "x"},
    )
    result = await apply_association_update(db_session, resource, body)
    assert result.warnings == []
    await db_session.flush()

    assert resource.series_id == series.id
    assert resource.movie_id is None
    assert resource.audio_work_id is None
    # 显式发送了集数字段 → confidence 置 manual（ambiguous 被修正）
    assert resource.episode_confidence == "manual"
    assert resource.season == 1 and resource.episode == 5
    # 合集字段清空
    assert resource.batch_scope is None and resource.season_ranges is None
    # 旧映射被删除；非合集分支不落任何映射（载荷中的 placements 一并清除）
    assert list(resource.file_assignments) == []
    # 媒体字段显式键生效；未知键被忽略不报错
    assert resource.resolution == "2160p"
    assert not resource.work_links


async def test_non_batch_movie_work_sets_movie_fk(db_session):
    movie = await _seed_movie(db_session)
    resource = await _seed_resource(db_session)
    body = ResourceAssociationUpdateRequest(
        is_batch=False, works=[_ref(movie)], assignments=[],
    )
    await apply_association_update(db_session, resource, body)
    await db_session.flush()
    assert resource.movie_id == movie.id
    assert resource.series_id is None


async def test_non_batch_no_works_keeps_existing_linkage(db_session):
    """纯媒体字段保存不得解除既有单作品挂载。"""
    series = await _seed_series(db_session)
    resource = await _seed_resource(db_session, series_id=series.id)
    body = ResourceAssociationUpdateRequest(
        is_batch=False, works=[],
        fields={"subtitle_langs": ["zh-CN"]},
    )
    await apply_association_update(db_session, resource, body)
    await db_session.flush()
    assert resource.series_id == series.id
    assert resource.subtitle_langs == ["zh-CN"]


async def test_transient_resource_refresh_failure_is_tolerated(db_session):
    """游离实例 refresh 失败走容错分支，流程照常完成。"""
    transient = FileResource(
        id=_uuid(), channel_id=_uuid(), guid=_uuid(),
        title_raw="raw", torrent_url="magnet:?xt=urn:btih:x",
    )
    body = ResourceAssociationUpdateRequest(is_batch=False, works=[])
    result = await apply_association_update(db_session, transient, body)
    assert result.warnings == []


# ---------------------------------------------------------------------------
# diff-preserving 映射替换
# ---------------------------------------------------------------------------


async def test_assignment_replace_preserves_provenance(db_session):
    series = await _seed_series(db_session)
    resource = await _seed_resource(db_session)
    keep = ResourceFileAssignment(
        resource_id=resource.id, file_path="keep.mkv",
        series_id=series.id, season=1, episode_start=1, episode_end=1,
        file_size=10, source="auto",
    )
    drop = ResourceFileAssignment(
        resource_id=resource.id, file_path="drop.mkv",
        series_id=series.id, season=1, episode_start=2, episode_end=2,
        source="llm",
    )
    db_session.add_all([keep, drop])
    await db_session.flush()

    body = ResourceAssociationUpdateRequest(
        is_batch=True,
        works=[_ref(series)],
        assignments=[
            # keep.mkv 完全相同 → 保留 auto 来源
            _asg("keep.mkv", series, season=1,
                 episode_start=1, episode_end=1, file_size=99),
            # 新增 → manual
            _asg("new.mkv", series, season=1,
                 episode_start=3, episode_end=3),
            # drop.mkv 不在载荷中 → 删除
        ],
    )
    await apply_association_update(db_session, resource, body)
    await db_session.flush()
    rows = {a.file_path: a for a in resource.file_assignments}
    assert set(rows) == {"keep.mkv", "new.mkv"}
    assert rows["keep.mkv"].source == "auto"
    # 完全相同的放置短路保留（连 file_size 快照都不动）
    assert rows["keep.mkv"].file_size == 10
    assert rows["new.mkv"].source == "manual"


# ---------------------------------------------------------------------------
# 合集分支：FK 镜像 / scope 推导 / links
# ---------------------------------------------------------------------------


async def test_batch_single_tv_mirrors_fk_and_derives_season(db_session):
    series = await _seed_series(db_session)
    resource = await _seed_resource(db_session)
    body = ResourceAssociationUpdateRequest(
        is_batch=True,
        works=[_ref(series)],
        assignments=[
            _asg("e01.mkv", series, season=1, episode_start=1, episode_end=2),
            _asg("e03.mkv", series, season=1, episode_start=3, episode_end=3),
        ],
    )
    await apply_association_update(db_session, resource, body)
    await db_session.flush()
    # 恰一作品 → 镜像进互斥 FK（dedup coverage key 依赖）
    assert resource.series_id == series.id and resource.movie_id is None
    assert resource.is_batch is True and resource.episode is None
    assert resource.batch_scope == "season"
    assert resource.batch_seasons == [1]
    assert resource.season_ranges == [
        {"season": 1, "episode_start": 1, "episode_end": 3},
    ]
    links = list(resource.work_links)
    assert len(links) == 1 and links[0].series_id == series.id


async def test_batch_multi_tv_derives_franchise_and_clears_fk(db_session):
    s1, s2 = await _seed_series(db_session, "A"), await _seed_series(db_session, "B")
    collection = WorkCollection(
        id=_uuid(), title_cn="A+B 合集", external_source="tmdb_collection",
        external_id="42",
    )
    db_session.add(collection)
    resource = await _seed_resource(db_session, series_id=s1.id)
    await db_session.flush()
    body = ResourceAssociationUpdateRequest(
        is_batch=True,
        works=[_ref(s1), _ref(s2)],
        collection_id=collection.id,
        assignments=[
            _asg("a.mkv", s1, season=1, episode_start=1, episode_end=1),
            _asg("b.mkv", s2, season=1, episode_start=1, episode_end=1),
        ],
    )
    await apply_association_update(db_session, resource, body)
    await db_session.flush()
    assert resource.batch_scope == "franchise"
    assert resource.series_id is None and resource.movie_id is None
    assert resource.collection_id == collection.id
    assert sorted(ln.series_id for ln in resource.work_links) == sorted([s1.id, s2.id])


async def test_batch_all_movies_derives_movies_scope(db_session):
    m1, m2 = await _seed_movie(db_session, "M1"), await _seed_movie(db_session, "M2")
    resource = await _seed_resource(db_session)
    body = ResourceAssociationUpdateRequest(
        is_batch=True,
        works=[_ref(m1), _ref(m2)],
        assignments=[
            _asg("m1.mkv", m1),
            _asg("m2.mkv", m2),
        ],
    )
    await apply_association_update(db_session, resource, body)
    await db_session.flush()
    assert resource.batch_scope == "movies"
    assert resource.movie_id is None and resource.series_id is None


async def test_batch_mixed_tv_movie_derives_franchise(db_session):
    s1, m1 = await _seed_series(db_session), await _seed_movie(db_session)
    resource = await _seed_resource(db_session)
    body = ResourceAssociationUpdateRequest(
        is_batch=True,
        works=[_ref(s1), _ref(m1)],
        assignments=[
            _asg("tv.mkv", s1, season=2, episode_start=1, episode_end=1),
            _asg("mv.mkv", m1),
        ],
    )
    await apply_association_update(db_session, resource, body)
    await db_session.flush()
    assert resource.batch_scope == "franchise"


async def test_batch_no_works_falls_back_to_existing_scope(db_session):
    resource = await _seed_resource(db_session, batch_scope="season")
    body = ResourceAssociationUpdateRequest(
        is_batch=True, works=[], assignments=[],
    )
    await apply_association_update(db_session, resource, body)
    await db_session.flush()
    assert resource.batch_scope == "season"
    # 全新资源（无 scope）→ franchise 兜底
    fresh = await _seed_resource(db_session)
    body2 = ResourceAssociationUpdateRequest(is_batch=True, works=[], assignments=[])
    await apply_association_update(db_session, fresh, body2)
    await db_session.flush()
    assert fresh.batch_scope == "franchise"


async def test_batch_marks_stale_ambiguous_confidence_manual(db_session):
    series = await _seed_series(db_session)
    resource = await _seed_resource(db_session, episode_confidence="ambiguous")
    body = ResourceAssociationUpdateRequest(
        is_batch=True, works=[_ref(series)],
        assignments=[_asg("e01.mkv", series, season=1,
                          episode_start=1, episode_end=1)],
    )
    await apply_association_update(db_session, resource, body)
    assert resource.episode_confidence == "manual"


async def test_batch_multi_season_evidence_from_assignments(db_session):
    series = await _seed_series(db_session)
    resource = await _seed_resource(db_session)
    body = ResourceAssociationUpdateRequest(
        is_batch=True, works=[_ref(series)],
        assignments=[
            _asg("s1.mkv", series, season=1, episode_start=1, episode_end=1),
            _asg("s2.mkv", series, season=2, episode_start=1, episode_end=1),
        ],
    )
    await apply_association_update(db_session, resource, body)
    await db_session.flush()
    assert resource.batch_scope == "multi_season"
    assert resource.batch_seasons == [1, 2]


# ---------------------------------------------------------------------------
# 校验：硬错误与软警告
# ---------------------------------------------------------------------------


async def test_duplicate_work_refs_rejected(db_session):
    series = await _seed_series(db_session)
    resource = await _seed_resource(db_session)
    body = ResourceAssociationUpdateRequest(
        is_batch=False, works=[_ref(series), _ref(series)],
    )
    with pytest.raises(AssociationValidationError, match="重复项"):
        await apply_association_update(db_session, resource, body)


async def test_unknown_work_rejected(db_session):
    resource = await _seed_resource(db_session)
    ghost = AssociationWorkRef(work_type="series", work_id=_uuid())
    body = ResourceAssociationUpdateRequest(is_batch=False, works=[ghost])
    with pytest.raises(AssociationValidationError, match="作品不存在"):
        await apply_association_update(db_session, resource, body)


async def test_non_batch_with_two_works_rejected(db_session):
    s1, m1 = await _seed_series(db_session), await _seed_movie(db_session)
    resource = await _seed_resource(db_session)
    body = ResourceAssociationUpdateRequest(
        is_batch=False, works=[_ref(s1), _ref(m1)],
    )
    with pytest.raises(AssociationValidationError, match="至多关联一个作品"):
        await apply_association_update(db_session, resource, body)


async def test_missing_collection_rejected(db_session):
    resource = await _seed_resource(db_session)
    body = ResourceAssociationUpdateRequest(
        is_batch=True, works=[], collection_id=_uuid(),
    )
    with pytest.raises(AssociationValidationError, match="作品集不存在"):
        await apply_association_update(db_session, resource, body)


async def test_assignment_validation_errors(db_session):
    series = await _seed_series(db_session)
    other = await _seed_series(db_session, "另一部")
    resource = await _seed_resource(db_session)

    def _body(*asgs) -> ResourceAssociationUpdateRequest:
        return ResourceAssociationUpdateRequest(
            is_batch=True, works=[_ref(series)], assignments=list(asgs),
        )

    with pytest.raises(AssociationValidationError, match="路径不能为空"):
        await apply_association_update(
            db_session, resource, _body(_asg("  ", series)))
    with pytest.raises(AssociationValidationError, match="重复映射"):
        await apply_association_update(
            db_session, resource,
            _body(_asg("a.mkv", series, season=1),
                  _asg("a.mkv", series, season=2)))
    with pytest.raises(AssociationValidationError, match="不在作品关联列表"):
        await apply_association_update(
            db_session, resource, _body(_asg("x.mkv", other, season=1)))
    with pytest.raises(AssociationValidationError, match="必须指定季"):
        await apply_association_update(
            db_session, resource, _body(_asg("y.mkv", series)))


async def test_overlapping_runs_rejected_and_gap_warned(db_session):
    series = await _seed_series(db_session)
    resource = await _seed_resource(db_session)

    overlap = ResourceAssociationUpdateRequest(
        is_batch=True, works=[_ref(series)],
        assignments=[
            _asg("e01.mkv", series, season=1,
                 episode_start=1, episode_end=3),
            _asg("e02.mkv", series, season=1,
                 episode_start=3, episode_end=4),
        ],
    )
    with pytest.raises(AssociationValidationError, match="区间重叠"):
        await apply_association_update(db_session, resource, overlap)

    gap = ResourceAssociationUpdateRequest(
        is_batch=True, works=[_ref(series)],
        assignments=[
            _asg("e01.mkv", series, season=1,
                 episode_start=1, episode_end=2),
            _asg("e05.mkv", series, season=1,
                 episode_start=5, episode_end=6),
        ],
    )
    result = await apply_association_update(db_session, resource, gap)
    assert any("断档" in w for w in result.warnings)
