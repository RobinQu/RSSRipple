"""P6 向导校验回放：用 fixture 中的真实合集包资源回放 PUT associations 的
完整性校验（service 层 ``apply_association_update``——API 层把
``AssociationValidationError`` 映射为 422 VALIDATION_ERROR，该映射由
tests/api 既有用例覆盖）。

用例（全部在迁移后的 fixture 库上运行）：

1. 缺作品指派 → 422：fixture 里真实存在的双作品 season 包
   （3e4c31bd，简体/繁体两个同 IP 作品挂同一资源，全部 12 条 assignment
   都绑在其中一部作品上）——原样回放其关联状态必须被「每个关联作品都必须
   有文件指派」拦下。
2. 两级关联不一致 → 422：collection_id 与作品的 collection_id 不一致。
3. 断档 → warning：真实 season 包的指派抽掉中间一集，提交成功但
   warnings 含「断档」。
4. 正向回放：单作品 season 包按现状重放（works + 全部 assignments）→
   通过校验且保持镜像 FK。
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.models.file_resource import FileResource
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

pytestmark = pytest.mark.asyncio(loop_scope="module")

# 双作品 season 包（简/繁同 IP 两个作品，12 条 assignment 全绑一部作品）。
MULTI_WORK_PACK_ID = "3e4c31bd-4cb0-4d62-954d-88ca2c36041b"


async def _load_resource(db, resource_id: str) -> FileResource:
    return (
        await db.execute(
            select(FileResource)
            .where(FileResource.id == resource_id)
            .options(
                selectinload(FileResource.work_links),
                selectinload(FileResource.file_assignments),
            )
        )
    ).scalar_one()


def _works_body(resource: FileResource) -> list[AssociationWorkRef]:
    return [
        AssociationWorkRef(
            work_type="series" if link.series_id else "movie",
            work_id=link.series_id or link.movie_id,
        )
        for link in resource.work_links
    ]


def _assignments_body(resource: FileResource) -> list[AssociationFileAssignment]:
    return [
        AssociationFileAssignment(
            file_path=row.file_path,
            work_type="series" if row.series_id else "movie",
            work_id=row.series_id or row.movie_id,
            file_size=row.file_size,
            season=row.season,
            episode_start=row.episode_start,
            episode_end=row.episode_end,
        )
        for row in resource.file_assignments
    ]


async def test_multi_work_pack_missing_work_assignment_422(migrated_db, db):
    """缺作品指派 422：原样回放该双作品包的现状（一部作品零指派）。"""
    resource = await _load_resource(db, MULTI_WORK_PACK_ID)
    assert len(resource.work_links) == 2
    assigned = {row.series_id for row in resource.file_assignments}
    linked = {link.series_id for link in resource.work_links}
    assert linked - assigned, "fixture 前提：存在一个零指派的关联作品"

    body = ResourceAssociationUpdateRequest(
        is_batch=True,
        works=_works_body(resource),
        assignments=_assignments_body(resource),
    )
    with pytest.raises(AssociationValidationError, match="每个关联作品都必须有文件指派"):
        await apply_association_update(db, resource, body)


async def test_two_level_collection_mismatch_422(migrated_db, db):
    """两级关联不一致 422：collection_id 不是作品所属合集。"""
    resource = await _load_resource(db, MULTI_WORK_PACK_ID)
    works = (
        await db.execute(
            select(TVSeries).where(
                TVSeries.id.in_([link.series_id for link in resource.work_links])
            )
        )
    ).scalars().all()
    own_collections = {w.collection_id for w in works}
    other = (
        await db.execute(
            select(WorkCollection).where(WorkCollection.id.not_in(own_collections))
        )
    ).scalars().first()
    assert other is not None

    body = ResourceAssociationUpdateRequest(
        is_batch=True,
        collection_id=other.id,
        works=_works_body(resource),
        assignments=_assignments_body(resource),
    )
    with pytest.raises(AssociationValidationError, match="必须属于所选合集"):
        await apply_association_update(db, resource, body)


async def test_episode_gap_warns(migrated_db, db):
    """断档 warning：真实 season 包抽掉中间一集 → 提交成功 + 断档警告。"""
    # 找一个有 ≥3 条连续集号 assignments 的单作品 season 包。
    candidates = (
        await db.execute(
            select(FileResource)
            .where(
                FileResource.is_batch.is_(True),
                FileResource.batch_scope == "season",
                FileResource.series_id.is_not(None),
            )
            .options(
                selectinload(FileResource.work_links),
                selectinload(FileResource.file_assignments),
            )
        )
    ).scalars().all()
    resource = next(
        r
        for r in candidates
        if len(r.work_links) <= 1
        and len({a.episode_start for a in r.file_assignments}) >= 3
    )
    by_episode = sorted(
        (a for a in resource.file_assignments if a.episode_start is not None),
        key=lambda a: a.episode_start,
    )
    # 抽掉中间一集制造断档。
    kept = [by_episode[0], by_episode[-1]]
    assert by_episode[-1].episode_start - by_episode[0].episode_start >= 2

    body = ResourceAssociationUpdateRequest(
        is_batch=True,
        works=[
            AssociationWorkRef(work_type="series", work_id=resource.series_id)
        ],
        assignments=[
            AssociationFileAssignment(
                file_path=a.file_path,
                work_type="series",
                work_id=a.series_id,
                season=a.season,
                episode_start=a.episode_start,
                episode_end=a.episode_end,
            )
            for a in kept
        ],
    )
    result = await apply_association_update(db, resource, body)
    assert any("断档" in w for w in result.warnings)


async def test_single_work_pack_full_replay_passes(migrated_db, db):
    """正向回放：双作品包只保留实际持有文件的那部作品 + 全部 12 条
    assignments → 通过校验，镜像 FK 保持，scope 维持 season。"""
    resource = await _load_resource(db, MULTI_WORK_PACK_ID)
    holder = next(
        link.series_id
        for link in resource.work_links
        if any(a.series_id == link.series_id for a in resource.file_assignments)
    )
    body = ResourceAssociationUpdateRequest(
        is_batch=True,
        works=[AssociationWorkRef(work_type="series", work_id=holder)],
        assignments=[
            a for a in _assignments_body(resource) if a.work_id == holder
        ],
    )
    result = await apply_association_update(db, resource, body)
    assert isinstance(result.warnings, list)
    await db.flush()
    assert resource.series_id == holder  # 恰一作品镜像 FK
    assert resource.batch_scope == "season"
    # 链接表收敛到单一作品。
    assert {link.series_id for link in resource.work_links} == {holder}
