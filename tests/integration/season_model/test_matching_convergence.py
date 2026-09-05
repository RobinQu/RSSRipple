"""P6 匹配收敛回归：迁移后新资源的 wikipedia / bangumi 路径收敛到同一季作品。

用 fixture 中的 ``metadata_cache`` 行做无 LLM 确定性重放：缓存键为
``(title, source=metadata_agent:<source>)``，``UnifiedMetadataAgent.process``
的缓存命中分支直接应用缓存判定（``_apply_to_resource`` →
``create_or_update_series_from_external``），全程零 LLM 调用。

- bangumi 路径（逐季源）：缓存命中 ``bangumi:501963`` → 作品袋反查 → 无职转生
  S3 季作品（P6 身份归属增强落位），不新建作品。
- wikipedia 路径（系列级源）：缓存命中 ``wikipedia:zh:8498329`` → 合集袋反查 →
  按 season hint 选 S3 成员 → 同一 S3 季作品。fixture 里 wikipedia 命名空间的
  缓存行 generation 均低于当前 ``METADATA_CACHE_GENERATION``（会被代际门禁
  丢弃并转 LLM 重跑），测试在加载后的 DB 里把该行的 generation 升到当前代
  （fixture JSON 本身不动）——重放的是同一份 matched_entity。
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from app.models.file_resource import FileResource
from app.models.metadata_cache import METADATA_CACHE_GENERATION, MetadataCache
from app.models.series import TVSeries
from app.models.work_collection import WorkCollection

pytestmark = pytest.mark.asyncio(loop_scope="module")

MUSHOKU_ID = "303bca1f-179e-41d4-a965-0d703c99ebd0"
MIKAN_CHANNEL_ID = "98dba1db-3ede-49f5-b441-42872e866798"  # metadata_source=bangumi

# fixture 中真实存在且带 gen-5 bangumi 缓存（found → bangumi:501963）的 raw title。
BANGUMI_TITLE = (
    "[LoliHouse] 无职转生 3期 / Mushoku Tensei S3 - 10 "
    "[WebRip 1080p HEVC-10bit AAC][简繁内封字幕]"
)
# fixture 中真实存在且带 gen-4 wikipedia 缓存（found → wikipedia:zh:8498329）的
# raw title（测试内升代后重放）。
WIKIPEDIA_TITLE = (
    "[Nix-Raws] 无职转生 第三季 ～到了异世界就拿出真本事～ "
    "/ 无职転生Ⅲ ～异世界行ったら本気だす～ / Mushoku Tensei S03E08 "
    "[CATCHPLAY WEB-DL 1080p AVC AAC][简繁内封]"
)


async def _s3_work(db) -> TVSeries:
    original = await db.get(TVSeries, MUSHOKU_ID)
    assert original is not None and original.collection_id is not None
    members = (
        await db.execute(
            select(TVSeries).where(TVSeries.collection_id == original.collection_id)
        )
    ).scalars().all()
    by_season = {m.season_number: m for m in members}
    assert set(by_season) == {1, 2, 3}
    return by_season[3]


async def _series_count(db) -> int:
    return int((await db.execute(select(func.count()).select_from(TVSeries))).scalar_one())


def _new_resource(channel_id: str, raw_title: str) -> FileResource:
    """Simulate the NEXT release of the same show (S3E11) reusing a cached raw
    title verbatim as the deterministic replay key."""
    return FileResource(
        id=str(uuid.uuid4()),
        channel_id=channel_id,
        guid=str(uuid.uuid4()),
        title_raw=raw_title,
        torrent_url="magnet:?xt=urn:btih:replays3e11",
        search_title="无职转生 第三季 ～到了异世界就拿出真本事～",
        season=3,
        episode=11,
        is_batch=False,
    )


async def _process(db, resource, channel) -> None:
    from app.services.metadata_agent import UnifiedMetadataAgent

    agent = UnifiedMetadataAgent()
    await agent.process(resource, channel, db)


async def test_bangumi_path_converges_to_s3_work(migrated_db, db):
    """bangumi 缓存重放：袋命中 bangumi:501963 → S3 季作品，不新建作品。"""
    from app.models.channel import Channel

    s3 = await _s3_work(db)
    before = await _series_count(db)

    channel = await db.get(Channel, MIKAN_CHANNEL_ID)
    assert channel is not None and channel.metadata_source == "bangumi"
    resource = _new_resource(channel.id, BANGUMI_TITLE)
    db.add(resource)
    await db.flush()

    await _process(db, resource, channel)

    assert resource.series_id == s3.id
    assert resource.movie_id is None
    assert resource.metadata_matched_at is not None
    # 季号不被缓存判定覆盖（缓存 meta 无季号），资源保持 S3E11。
    assert (resource.season, resource.episode) == (3, 11)
    assert await _series_count(db) == before


async def test_wikipedia_path_converges_to_same_s3_work(migrated_db, db):
    """wikipedia 缓存重放（升代后）：系列级 id → 合集袋 → S3 成员，与 bangumi
    路径收敛到同一季作品——本次事故（同剧裂成两个互不相认的作品）的防复现
    回归。"""
    from app.models.channel import Channel

    s3 = await _s3_work(db)
    before = await _series_count(db)

    # 升代：fixture 的 wikipedia 缓存行 generation < 当前代，测试 DB 内升级到
    # 当前代以重放同一份 matched_entity（fixture JSON 不动）。
    cache_row = (
        await db.execute(
            select(MetadataCache).where(
                MetadataCache.title == WIKIPEDIA_TITLE.strip(),
                MetadataCache.source == "metadata_agent:wikipedia",
            )
        )
    ).scalar_one()
    assert (cache_row.metadata_json or {}).get("found") is True
    assert (
        (cache_row.metadata_json["matched_entity"] or {}).get("external_id")
        == "wikipedia:zh:8498329"
    )
    cache_row.generation = METADATA_CACHE_GENERATION

    # fixture 频道无 wikipedia 源——建一个测试频道（不动 fixture 数据）。
    channel = Channel(
        id=str(uuid.uuid4()),
        name="replay-wikipedia",
        type="rss_feed",
        url="https://example.com/replay-wikipedia",
        fetch_interval=1800,
        status="active",
        field_mapping={
            "list_locator": {"source": "entries"},
            "field_mappings": {"torrent_url": {"source": "link"}},
        },
        metadata_agent_enabled=True,
        metadata_source="wikipedia",
    )
    db.add(channel)
    await db.flush()

    resource = _new_resource(channel.id, WIKIPEDIA_TITLE)
    db.add(resource)
    await db.flush()

    await _process(db, resource, channel)

    assert resource.series_id == s3.id
    assert resource.metadata_matched_at is not None
    assert (resource.season, resource.episode) == (3, 11)
    # 合集袋命中后未新建任何作品/合集。
    assert await _series_count(db) == before
    collection = await db.get(WorkCollection, s3.collection_id)
    assert collection is not None
