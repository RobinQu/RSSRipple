"""P6 迁移不变量集成验证：生产 fixture → 全量迁移 → verify 三项检查。

加载真实生产对象图（65 series / 22 movies / 559 resources / 1517 episodes /
213 links / 3765 assignments），进程内跑 ``run_migration(apply=True)``，然后:

- verify 脚本的三项检查（悬空 FK / 季一致性 / search_text）必须零问题；
- 资源 / 订阅 / 决策 / 映射 / 电影行数守恒，集数与关联行只减不增
  （碰撞合并是唯一合法收缩），作品/合集/身份袋可增长；
- 每部被拆分的作品，其合集成员的季集合等于 fixture 数据独立推导的
  季集合（seasons JSON ∪ Episode 行 ∪ 资源 season/batch_seasons）；
- 无职转生样本：``bangumi:501963`` 落在 S3 季作品袋（P6 逐季身份归属
  增强）、115 条 S3 资源全部指向 S3 作品、逐季集数正确路由；
- 无法定位的资源停泊合集（series_id 清空、collection_id 设置）；
- 迁移幂等：重跑全部 skipped，行数不再变化。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from app.models.agent_work import AgentWork
from app.models.channel_raw_title_mapping import ChannelRawTitleMapping
from app.models.episode import Episode
from app.models.file_resource import FileResource
from app.models.movie import Movie
from app.models.pending_decision import PendingDecision
from app.models.resource_file_assignment import ResourceFileAssignment
from app.models.resource_work_link import ResourceWorkLink
from app.models.series import TVSeries
from app.models.work_collection import WorkCollection
from app.models.work_external_id import WorkExternalId
from scripts.verify_season_split import (
    _check_consistency,
    _check_dangling_fks,
    _check_search_text,
)

from .conftest import run_full_migration

pytestmark = pytest.mark.asyncio(loop_scope="module")

# 本次事故的回归样本（合并后形态的系列级行，资源全部在 S3）。
MUSHOKU_ID = "303bca1f-179e-41d4-a965-0d703c99ebd0"

_COUNT_MODELS = {
    "tv_series": TVSeries,
    "movies": Movie,
    "work_collections": WorkCollection,
    "episodes": Episode,
    "file_resources": FileResource,
    "resource_work_links": ResourceWorkLink,
    "resource_file_assignments": ResourceFileAssignment,
    "agent_works": AgentWork,
    "pending_decisions": PendingDecision,
    "channel_raw_title_mappings": ChannelRawTitleMapping,
    "work_external_ids": WorkExternalId,
}

# 迁移必须严格守恒的表（与 scripts/verify_season_split.py 一致）。
_CONSERVED = {
    "movies",
    "file_resources",
    "agent_works",
    "pending_decisions",
    "channel_raw_title_mappings",
}
# 碰撞合并（adopt-existing / uq 折叠）只允许这些表收缩。
_SHRINK_ONLY = {"episodes", "resource_file_assignments"}
# resource_work_links 不守恒：multi_season 包的单条 legacy link 会按季展开为
# 每条季作品一条（links_created），uq 碰撞又可能折叠——只作信息对比。


async def _counts(db) -> dict[str, int]:
    out = {}
    for name, model in _COUNT_MODELS.items():
        out[name] = int(
            (await db.execute(select(func.count()).select_from(model))).scalar_one()
        )
    return out


def _expected_season_sets(data: dict) -> dict[str, set[int]]:
    """Independent re-derivation of every fixture series' season set from the
    fixture JSON (migration spec step c: seasons JSON ∪ Episode 行 ∪ 资源
    season/batch_seasons)."""
    series_ids = {s["id"] for s in data["tables"]["tv_series"]}
    sets: dict[str, set[int]] = {sid: set() for sid in series_ids}
    for s in data["tables"]["tv_series"]:
        for entry in s.get("seasons") or []:
            if isinstance(entry, dict) and isinstance(entry.get("season_number"), int):
                sets[s["id"]].add(entry["season_number"])
    for ep in data["tables"]["episodes"]:
        if ep["series_id"] in sets and isinstance(ep.get("season"), int):
            sets[ep["series_id"]].add(ep["season"])
    for r in data["tables"]["file_resources"]:
        sid = r.get("series_id")
        if sid not in sets:
            continue
        if isinstance(r.get("season"), int):
            sets[sid].add(r["season"])
        for value in r.get("batch_seasons") or []:
            if isinstance(value, int):
                sets[sid].add(value)
    return sets


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def migration(fixture_db):
    """Capture pre-migration counts, apply the full migration, keep reports."""
    async with fixture_db.factory() as session:
        before = await _counts(session)
    reports = await run_full_migration()
    async with fixture_db.factory() as session:
        after = await _counts(session)
    return SimpleNamespace(
        factory=fixture_db.factory,
        data=fixture_db.data,
        reports=reports,
        before=before,
        after=after,
    )


async def test_verify_checks_pass(migration, db):
    """verify 脚本三项检查（悬空 FK / 季一致性 / search_text）零问题。"""
    assert await _check_dangling_fks(db) == []
    assert await _check_consistency(db) == []
    assert await _check_search_text(db) == []


async def test_row_count_conservation(migration):
    before, after = migration.before, migration.after
    for name in _CONSERVED:
        assert after[name] == before[name], (
            f"{name} must be conserved: {before[name]} → {after[name]}"
        )
    for name in _SHRINK_ONLY:
        assert after[name] <= before[name], (
            f"{name} may only shrink by collision dedup: {before[name]} → {after[name]}"
        )


async def test_split_season_sets_match_fixture_evidence(migration, db):
    """每部被拆作品的合集成员季集合 == fixture 独立推导的季集合。"""
    expected = _expected_season_sets(migration.data)
    split_reports = [r for r in migration.reports if r.status == "split"]
    # fixture 中至少有 6 部资源实际跨季的作品 + 声明多季的作品被拆。
    assert len(split_reports) >= 6
    for report in split_reports:
        assert set(report.seasons) == expected[report.series_id], (
            f"{report.title!r}: season set {report.seasons} != fixture-derived "
            f"{sorted(expected[report.series_id])}"
        )
        members = (
            await db.execute(
                select(TVSeries).where(TVSeries.collection_id == report.collection_id)
            )
        ).scalars().all()
        member_seasons = {m.season_number for m in members}
        assert set(report.seasons) <= member_seasons, (
            f"{report.title!r}: seasons {report.seasons} missing from members "
            f"{sorted(member_seasons)}"
        )
        # 拆分产物不制造重复 (collection, season) 成员。
        assert len(members) == len(member_seasons)

    # 资源实际跨季的 6 部作品全部拆分出正确的季作品数。
    data = migration.data
    by_series: dict[str, set[int]] = {}
    for r in data["tables"]["file_resources"]:
        if r.get("series_id") and isinstance(r.get("season"), int):
            by_series.setdefault(r["series_id"], set()).add(r["season"])
    cross_season = {sid for sid, seasons in by_series.items() if len(seasons) > 1}
    assert len(cross_season) == 6
    reports_by_id = {r.series_id: r for r in migration.reports}
    for sid in cross_season:
        report = reports_by_id[sid]
        assert report.status == "split", f"{report.title!r} not split"
        assert set(report.seasons) == expected[sid]


async def test_mushoku_tensei_sample(migration, db):
    """无职转生事故样本：bangumi 逐季身份落 S3、资源/集数正确路由。"""
    report = next(r for r in migration.reports if r.series_id == MUSHOKU_ID)
    assert report.status == "split"
    assert set(report.seasons) == {1, 2, 3}

    original = await db.get(TVSeries, MUSHOKU_ID)
    assert original is not None
    assert original.collection_id == report.collection_id
    # 原行复用为锚点季（S1）。
    assert original.season_number == 1

    members = (
        await db.execute(
            select(TVSeries).where(TVSeries.collection_id == report.collection_id)
        )
    ).scalars().all()
    by_season = {m.season_number: m for m in members}
    assert set(by_season) == {1, 2, 3}
    s3 = by_season[3]

    # P6 增强：袋中 bangumi:501963 归属 S3 季作品，不再滞留 S1。
    s3_bag = (
        await db.execute(
            select(WorkExternalId.external_id).where(
                WorkExternalId.work_type == "series",
                WorkExternalId.work_id == s3.id,
            )
        )
    ).scalars().all()
    s1_bag = (
        await db.execute(
            select(WorkExternalId.external_id).where(
                WorkExternalId.work_type == "series",
                WorkExternalId.work_id == original.id,
            )
        )
    ).scalars().all()
    assert "bangumi:501963" in s3_bag
    assert "bangumi:501963" not in s1_bag
    assert "bangumi:501963" in report.season_identities_routed
    # 系列级身份搬到合集袋（兼容期主列不动）。
    coll_bag = (
        await db.execute(
            select(WorkExternalId.external_id).where(
                WorkExternalId.work_type == "collection",
                WorkExternalId.work_id == report.collection_id,
            )
        )
    ).scalars().all()
    assert "wikipedia:zh:8498329" in coll_bag
    assert (original.external_id, original.external_source) == (
        "wikipedia:zh:8498329",
        "wikipedia",
    )

    # 115 条 S3 资源全部指向 S3 季作品。
    fixture_ids = {
        r["id"]
        for r in migration.data["tables"]["file_resources"]
        if r.get("series_id") == MUSHOKU_ID
    }
    assert len(fixture_ids) == 115
    routed = (
        await db.execute(
            select(FileResource.series_id, func.count())
            .where(FileResource.id.in_(fixture_ids))
            .group_by(FileResource.series_id)
        )
    ).all()
    assert routed == [(s3.id, 115)]

    # 逐季集数路由：S1=23 / S2=25 / S3=14。
    for season, expected_count in ((1, 23), (2, 25), (3, 14)):
        count = int(
            (
                await db.execute(
                    select(func.count())
                    .select_from(Episode)
                    .where(Episode.series_id == by_season[season].id)
                )
            ).scalar_one()
        )
        assert count == expected_count, f"S{season} episodes {count} != {expected_count}"


async def test_indeterminate_resources_park_on_collection(migration, db):
    """无法定位季的资源停泊合集（清作品 FK、挂 collection_id）。"""
    parked = [pid for r in migration.reports for pid in r.parked_resources]
    for pid in parked:
        resource = await db.get(FileResource, pid)
        assert resource is not None
        assert resource.series_id is None
        assert resource.collection_id is not None


async def test_migration_idempotent_rerun(migration):
    """重跑收敛：全部 skipped，行数与首轮迁移后一致。"""
    reports = await run_full_migration()
    assert reports, "no series examined on rerun"
    assert {r.status for r in reports} == {"skipped"}
    async with migration.factory() as session:
        assert await _counts(session) == migration.after
        assert await _check_dangling_fks(session) == []
        assert await _check_consistency(session) == []
        assert await _check_search_text(session) == []
