"""P6 派发 / organize 等价性：迁移前后同一批资源的决策集合与整理目标路径。

双库结构：``pre`` 库保持迁移前状态，``post`` 库执行全量迁移 + runbook 补订阅
（订阅保持作品粒度——多季作品的其他季需补订 AgentWork，克隆原订阅行的
filter_overrides / enable_episode_dedup）。两边对同一批频道资源各跑一遍
``process_resources``（会话内 rollback，不落库）。

已刻画并钉住的分歧（P6 发现）：迁移后非锚点季作品 ``start_date`` 为 NULL
（规格：仅 S1 保留原值，其余待刷新），Channel 必选字段 ``year`` 门禁会把
这些季的资源拦进待确认。迁移脚本已尽力离线推导（本季 Episode 最早
air_date）；仍无证据的作品进入 pinned 集合 G——断言 G 中每条资源的门禁原因
恰好只有 ``('year',)`` 且其季作品 start_date 为 NULL、迁移前并未被拦。
剔除 G 后两边决策集合必须完全一致（计数器 / matched / dispatched /
PendingDecision 键）；全量口径下迁移后不得出现迁移前不存在的新冲突
（无新冲突误报）。

organize：``pre`` 库用冻结 v1 快照直接 ``build_plan``；``post`` 库走
``regenerate_notifications`` 全链路重建 v2 快照（mock 下载器 RPC 回放原文件
清单）再 ``build_plan``。断言每个 completed 任务的目标路径集合一致
（``Season NN`` 目录层级不变）。
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.models.agent import Agent
from app.models.agent_work import AgentWork
from app.models.download_notification import DownloadNotification
from app.models.download_task import DownloadTask
from app.models.file_resource import FileResource
from app.models.movie import Movie
from app.models.pending_decision import PendingDecision
from app.models.series import TVSeries
from app.services.agent_service import process_resources
from app.services.organize_planner import DiskFile, PlanError, build_plan

from .conftest import open_fixture_db, run_full_migration

pytestmark = pytest.mark.asyncio(loop_scope="module")

AGENT_ID = "e0c4892f-8f96-4499-846f-1e6e2bf073fc"

_TV_TEMPLATE = "{title}/Season {season:02d}/{title} - {episode_code}{ext}"
_LIBRARY_ID = "lib-integration"
_LIBRARY = SimpleNamespace(
    id=_LIBRARY_ID,
    root_path="/media/anime",
    recycle_path=None,
    subtitle_lang_map=None,
)
_RULE = SimpleNamespace(
    id="rule-integration",
    name="all-tv",
    priority=0,
    enabled=True,
    filter=None,
    library_id=_LIBRARY_ID,
    path_template=_TV_TEMPLATE,
    file_op="move",
)


def _transmission_patches():
    return [
        patch(
            "app.clients.transmission.TransmissionWrapper.add_torrent",
            AsyncMock(return_value={"torrent_id": 4242, "name": "x", "hash": "h"}),
        ),
        patch(
            "app.clients.transmission.TransmissionWrapper.pause_torrent",
            AsyncMock(return_value=True),
        ),
        patch(
            "app.clients.transmission.TransmissionWrapper.resume_torrent",
            AsyncMock(return_value=True),
        ),
    ]


async def _run_dispatch(factory, *, exclude: set[str] | None = None) -> dict:
    """一次 agent 全量运行的可对比记录（会话整体 rollback，不落库）。

    ``exclude``：从输入中剔除的资源 id（pinned year-门禁集合 G），用于
    迁移前后同口径对比。
    """
    async with factory() as db:
        agent = await db.get(Agent, AGENT_ID, options=[selectinload(Agent.channel)])
        assert agent is not None
        resources = list(
            (
                await db.execute(
                    select(FileResource)
                    .where(FileResource.channel_id == agent.channel_id)
                    .options(
                        selectinload(FileResource.series).selectinload(
                            TVSeries.collection
                        ),
                        selectinload(FileResource.movie).selectinload(Movie.collection),
                        selectinload(FileResource.collection),
                        selectinload(FileResource.work_links),
                    )
                    .order_by(FileResource.created_at.asc())
                )
            )
            .scalars()
            .all()
        )
        if exclude:
            resources = [r for r in resources if r.id not in exclude]
        preexisting_tasks = set(
            (
                await db.execute(
                    select(DownloadTask.id).where(DownloadTask.agent_id == agent.id)
                )
            )
            .scalars()
            .all()
        )
        preexisting_decisions = set(
            (
                await db.execute(
                    select(PendingDecision.id).where(
                        PendingDecision.agent_id == agent.id
                    )
                )
            )
            .scalars()
            .all()
        )
        for patcher in _transmission_patches():
            patcher.start()
        try:
            result = await process_resources(
                agent,
                resources,
                db,
                required_metadata_fields=(
                    agent.channel.required_metadata_fields if agent.channel else None
                ),
            )
        finally:
            patch.stopall()
        new_tasks = (
            await db.execute(
                select(DownloadTask).where(
                    DownloadTask.agent_id == agent.id,
                    DownloadTask.id.not_in(preexisting_tasks or {""}),
                )
            )
        ).scalars().all()
        new_decisions = (
            await db.execute(
                select(PendingDecision).where(
                    PendingDecision.agent_id == agent.id,
                    PendingDecision.id.not_in(preexisting_decisions or {""}),
                )
            )
        ).scalars().all()
        record = {
            "counters": {
                k: getattr(result, k)
                for k in (
                    "total_resources",
                    "matched",
                    "dispatched",
                    "pending_decisions",
                    "duplicates_skipped",
                    "unrecognized",
                    "filter_failed",
                )
            },
            "errors": sorted(result.errors),
            "matched_ids": sorted(result.matched_resource_ids),
            "dispatched_resources": sorted(
                (t.file_resource_id, t.status) for t in new_tasks
            ),
            # 决策键按 (episode, 候选资源集合) 归一——季号/作品 id 随季作品
            # 身份变化（终态：season 恒等于作品的 season_number）。
            "decision_keys": sorted(
                (d.episode, tuple(sorted(d.candidates or [])))
                for d in new_decisions
            ),
        }
        await db.rollback()
    return record


async def _clone_suggested_subscriptions(post, reports) -> int:
    """P8 runbook 步骤自动化：按迁移报告的 agent_suggestions 补订其他季
    作品，克隆锚点订阅行的 filter_overrides / enable_episode_dedup。"""
    added = 0
    async with post.factory() as db:
        for report in reports:
            for suggestion in report.agent_suggestions:
                source = (
                    await db.execute(
                        select(AgentWork).where(
                            AgentWork.agent_id == suggestion["agent_id"],
                            AgentWork.series_id == report.series_id,
                        )
                    )
                ).scalars().first()
                for target in suggestion["suggested"]:
                    db.add(
                        AgentWork(
                            id=str(uuid.uuid4()),
                            agent_id=suggestion["agent_id"],
                            series_id=target["work_id"],
                            content_type="tv",
                            filter_overrides=(
                                source.filter_overrides if source is not None else None
                            ),
                            enable_episode_dedup=(
                                source.enable_episode_dedup
                                if source is not None
                                else True
                            ),
                        )
                    )
                    added += 1
        await db.commit()
    return added


async def _year_gated_resources(post) -> dict[str, dict]:
    """迁移后仅因缺 year 被 Channel 门禁拦下的资源：{id: 证据 dict}。"""
    from app.services.resource_confirmation import inspect_resource_confirmation

    async with post.factory() as db:
        agent = await db.get(Agent, AGENT_ID, options=[selectinload(Agent.channel)])
        required = agent.channel.required_metadata_fields
        resources = (
            await db.execute(
                select(FileResource)
                .where(FileResource.channel_id == agent.channel_id)
                .options(
                    selectinload(FileResource.series),
                    selectinload(FileResource.work_links),
                )
            )
        ).scalars().all()
        gated: dict[str, dict] = {}
        for r in resources:
            conf = inspect_resource_confirmation(r, required)
            if (
                conf.required
                and conf.kinds == ("required_fields_missing",)
                and conf.missing_fields == ("year",)
            ):
                work = r.series
                gated[r.id] = {
                    "work_id": r.series_id,
                    "work_start_date": (
                        work.start_date.isoformat() if work and work.start_date else None
                    ),
                    "work_season": work.season_number if work else None,
                }
        return gated


def _disk_files(payload: dict) -> list[DiskFile]:
    """从快照的 files 清单合成磁盘文件（planner 为纯函数，不读盘）。"""
    task = payload.get("task") or {}
    root = f"/dl/{task.get('torrent_name')}" if task.get("torrent_name") else "/dl"
    return [
        DiskFile(path=f"{root}/{f['name']}", size=int(f.get("size") or 0), rel=f["name"])
        for f in (payload.get("files") or [])
    ]


def _plan_outcome(payload: dict):
    """(状态, 细节) — 可跨快照版本比较的计划结果。"""
    try:
        result = build_plan(
            payload, _disk_files(payload), [_RULE], {_LIBRARY_ID: _LIBRARY}
        )
    except PlanError as e:
        return ("error", str(e))
    dsts = sorted(op.dst for op in result.ops if op.dst)
    if result.uncategorized:
        return ("uncategorized", dsts)
    return ("ok", dsts)


async def _completed_notifications(db) -> dict[str, dict]:
    """completed 任务的 task_id → 通知 payload。"""
    rows = (
        await db.execute(
            select(DownloadTask, DownloadNotification)
            .join(
                DownloadNotification,
                DownloadNotification.download_task_id == DownloadTask.id,
            )
            .where(
                DownloadTask.agent_id == AGENT_ID,
                DownloadTask.status == "completed",
            )
        )
    ).all()
    return {task.id: notification.payload for task, notification in rows}


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def world(tmp_path_factory):
    """pre/post 双库世界：迁移前基线 + 迁移后重放 + pinned 门禁集合 G。"""
    async with open_fixture_db(
        tmp_path_factory.mktemp("pre") / "fixture.db"
    ) as pre:
        pre_full = await _run_dispatch(pre.factory)
        async with pre.factory() as db:
            pre_payloads = await _completed_notifications(db)
        pre_plans = {tid: _plan_outcome(p) for tid, p in pre_payloads.items()}

        async with open_fixture_db(
            tmp_path_factory.mktemp("post") / "fixture.db"
        ) as post:
            # 全局 factory 此时指向 post 库——run_full_migration 作用于 post。
            reports = await run_full_migration()
            subs_added = await _clone_suggested_subscriptions(post, reports)
            gated = await _year_gated_resources(post)

            g = set(gated)
            pre_reduced = await _run_dispatch(pre.factory, exclude=g)
            post_full = await _run_dispatch(post.factory)
            post_reduced = await _run_dispatch(post.factory, exclude=g)

            # 通知重生成：mock RPC 回放各任务冻结快照里的文件清单（不降级）。
            async with post.factory() as db:
                stored = await _completed_notifications(db)
                completed = (
                    await db.execute(
                        select(DownloadTask).where(
                            DownloadTask.agent_id == AGENT_ID,
                            DownloadTask.status == "completed",
                        )
                    )
                ).scalars().all()
            files_by_torrent = {
                task.transmission_torrent_id: {
                    "name": ((stored.get(task.id) or {}).get("task") or {}).get(
                        "torrent_name"
                    ),
                    "files": (stored.get(task.id) or {}).get("files") or [],
                }
                for task in completed
            }

            async def _fake_get_files(torrent_id):
                return files_by_torrent.get(torrent_id, {"name": None, "files": []})

            patches = _transmission_patches() + [
                patch(
                    "app.clients.transmission.TransmissionWrapper.get_torrent_files",
                    AsyncMock(side_effect=_fake_get_files),
                ),
                # 库内无 OrganizeRule/Library 行——regenerate 末尾的 organize
                # 计划重建与本测试无关，隔离开。
                patch(
                    "app.services.organize_service.plan_for_notifications",
                    AsyncMock(return_value=None),
                ),
            ]
            for patcher in patches:
                patcher.start()
            try:
                from app.services.notify_service import regenerate_notifications

                async with post.factory() as db:
                    regen_stats = await regenerate_notifications(db, AGENT_ID, None)
            finally:
                patch.stopall()

            async with post.factory() as db:
                post_payloads = await _completed_notifications(db)
            post_plans = {tid: _plan_outcome(p) for tid, p in post_payloads.items()}

            yield SimpleNamespace(
                pre_full=pre_full,
                pre_reduced=pre_reduced,
                post_full=post_full,
                post_reduced=post_reduced,
                gated=gated,
                subs_added=subs_added,
                regen_stats=regen_stats,
                pre_plans=pre_plans,
                post_plans=post_plans,
                post_payloads=post_payloads,
            )


async def test_year_gate_divergence_is_fully_characterized(world):
    """pinned 集合 G：每条恰好只因缺 year 被拦、季作品 start_date 为 NULL、
    且迁移前并未被拦（纯迁移产物，非存量问题）。"""
    assert world.gated, "预期存在 year 门禁分歧（start_date 待刷新的季作品）"
    for rid, evidence in world.gated.items():
        assert evidence["work_id"] is not None
        assert evidence["work_start_date"] is None
        assert evidence["work_season"] is not None
    # 全量口径：迁移后的 unrecognized 恰好 = 迁移前 unrecognized + |G|。
    assert (
        world.post_full["counters"]["unrecognized"]
        == world.pre_full["counters"]["unrecognized"] + len(world.gated)
    )


async def test_dispatch_equivalent_after_runbook_steps(world):
    """剔除 G 后（同口径）：计数器 / matched / dispatched / 决策集合全等。"""
    assert world.post_reduced["errors"] == world.pre_reduced["errors"] == []
    assert world.post_reduced["counters"] == world.pre_reduced["counters"]
    assert world.post_reduced["matched_ids"] == world.pre_reduced["matched_ids"]
    assert (
        world.post_reduced["dispatched_resources"]
        == world.pre_reduced["dispatched_resources"]
    )
    assert (
        world.post_reduced["decision_keys"] == world.pre_reduced["decision_keys"]
    )


async def test_no_new_conflicts_post_migration(world):
    """无新冲突误报：迁移后每个决策的候选集是迁移前同集决策候选集的子集
    （允许 G 中候选被门禁拦下而减少，绝不允许新增）。"""
    g = set(world.gated)
    pre_by_episode: dict[int, set[str]] = {}
    for episode, candidates in world.pre_full["decision_keys"]:
        pre_by_episode.setdefault(episode, set()).update(candidates)
    for episode, candidates in world.post_full["decision_keys"]:
        assert episode in pre_by_episode, (
            f"迁移后出现迁移前不存在的冲突决策：episode {episode}"
        )
        assert set(candidates) <= pre_by_episode[episode], (
            f"episode {episode} 的候选集出现迁移前不存在的资源"
        )
    # 迁移前的决策在迁移后要么仍在（候选可为子集），要么候选全被 G 拦下。
    post_by_episode = {ep for ep, _ in world.post_full["decision_keys"]}
    for episode, candidates in world.pre_full["decision_keys"]:
        if episode in post_by_episode:
            continue
        assert set(candidates) <= g, (
            f"迁移前 episode {episode} 的冲突决策在迁移后消失，"
            "但候选并非全部被 year 门禁拦下"
        )


async def test_organize_target_paths_equivalent(world):
    """completed 任务迁移前后 OrganizePlan 目标路径一致（Season NN 不变）。"""
    assert world.pre_plans, "fixture 中没有可规划的 completed 任务通知"
    assert set(world.post_plans) == set(world.pre_plans)
    for task_id, pre in world.pre_plans.items():
        post = world.post_plans[task_id]
        assert post == pre, (
            f"task {task_id[:8]}: 迁移前 {pre[0]} vs 迁移后 {post[0]}\n"
            f"  pre:  {pre[1]}\n  post: {post[1]}"
        )
    # 季目录层级确实被 exercised（v1/v2 快照都渲染出 Season NN）。
    all_dsts = [d for status, d in world.pre_plans.values() if status == "ok"]
    assert all_dsts, "所有计划都未产出目标路径"
    assert any("Season 01" in path for dsts in all_dsts for path in dsts)
    # v2 快照契约回归：重生成后 work 带 season_number、无 seasons 键。
    assert world.regen_stats["regenerated"] >= 1
    for payload in world.post_payloads.values():
        work = payload.get("work") or {}
        assert payload.get("version") == 2
        assert "seasons" not in work
        assert work.get("season_number") == 1  # 这 4 个任务均为 S1 作品
