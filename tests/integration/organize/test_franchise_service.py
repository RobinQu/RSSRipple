"""Franchise pack 成员链接服务（franchise_service）进程内集成测试。

覆盖 ``link_franchise_pack`` 的完整契约：成员解析（stub agent →
create_or_update_* upsert）、WorkCollection get-or-create（归一化标题
幂等）、已属其他合集的作品不被抢走、全部成员失败时仅保留 batch 判定、
FK 互斥清理与 _pack_title 的回退链。
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.models.channel import Channel
from app.models.file_resource import FileResource
from app.models.work_collection import WorkCollection
from app.services.franchise_service import (
    FRANCHISE_PACK_SOURCE,
    _pack_title,
    link_franchise_pack,
)
from app.services.torrent_inspect import TorrentReport


def _uuid() -> str:
    return str(uuid.uuid4())


_TEST_FIELD_MAPPING = {
    "list_locator": {"source": "entries"},
    "field_mappings": {"torrent_url": {"source": "link"}},
}


async def _seed_channel(db_session) -> Channel:
    ch = Channel(
        id=_uuid(), name="fr-ch", type="rss_feed",
        url="https://example.com/rss",
        field_mapping=_TEST_FIELD_MAPPING, metadata_agent_enabled=False,
    )
    db_session.add(ch)
    await db_session.flush()
    return ch


async def _seed_resource(db_session, channel, **kw) -> FileResource:
    resource = FileResource(
        id=_uuid(), channel_id=channel.id, guid=_uuid(),
        title_raw=kw.pop("title_raw", "[VCB] 某大IP 全家桶"),
        torrent_url="magnet:?xt=urn:btih:abc", **kw,
    )
    db_session.add(resource)
    await db_session.flush()
    return resource


def _report(*titles: str) -> TorrentReport:
    return TorrentReport(scope="franchise", is_batch=True,
                         work_titles=list(titles))


def _agent_returning(found=True, content_type="tv", title="作品A",
                     external_id="ext-a"):
    meta = SimpleNamespace(
        found=found,
        content_type=content_type,
        matched_entity={
            "content_type": content_type,
            "external_id": external_id,
            "external_source": "llm_search",
            "title_cn": title,
            "title_en": title,
            "original_title": title,
        } if found else None,
    )

    class _Agent:
        async def process_title_only(self, raw_title, source):
            # 标题与外部 id 都随标题变化：避免身份袋把多成员收敛到同一行
            entity = dict(meta.matched_entity or {},
                          title_cn=raw_title, title_en=raw_title,
                          original_title=raw_title)
            if found:
                entity["external_id"] = f"{external_id}-{raw_title}"
            return SimpleNamespace(found=found, content_type=content_type,
                                   matched_entity=entity)

    return _Agent()


def _patch_agent(agent):
    return patch(
        "app.services.metadata_agent.get_agent", return_value=agent
    )


_POSTER = "app.services.metadata_service.download_and_cache_poster"


# ---------------------------------------------------------------------------
# 主链路
# ---------------------------------------------------------------------------


async def test_link_franchise_pack_creates_collection_and_members(db_session):
    channel = await _seed_channel(db_session)
    from app.models.series import TVSeries
    stale = TVSeries(
        id=_uuid(), title_cn="旧链接", title_en="old",
        original_title="old", content_type="tv",
    )
    db_session.add(stale)
    await db_session.flush()
    resource = await _seed_resource(
        db_session, channel, search_title="某大IP", series_id=stale.id,
    )
    with _patch_agent(_agent_returning()), patch(_POSTER, new_callable=AsyncMock, return_value=None):
        await link_franchise_pack(db_session, resource, _report("作品A TV", "作品A 剧场版"), channel)
    await db_session.flush()

    assert resource.collection_id is not None
    coll = await db_session.get(WorkCollection, resource.collection_id)
    assert coll is not None
    assert coll.external_source == FRANCHISE_PACK_SOURCE
    assert coll.external_id is None
    assert "某大IP" in (coll.title_cn or "")
    # FK 互斥：collection 资源不带作品 FK
    assert resource.series_id is None and resource.movie_id is None
    # 成员作品挂到合集（TV + movie 各一）
    from sqlalchemy import select

    from app.models.movie import Movie
    from app.models.series import TVSeries
    tvs = (await db_session.execute(select(TVSeries))).scalars().all()
    movies = (await db_session.execute(select(Movie))).scalars().all()
    attached = [w for w in [*tvs, *movies] if w.collection_id == coll.id]
    assert len(attached) == 2


async def test_link_franchise_pack_is_idempotent(db_session):
    channel = await _seed_channel(db_session)
    resource = await _seed_resource(db_session, channel, search_title="某大IP")
    with _patch_agent(_agent_returning()), patch(_POSTER, new_callable=AsyncMock, return_value=None):
        await link_franchise_pack(db_session, resource, _report("作品A"), channel)
        first_id = resource.collection_id
        # 重跑：get-or-create 收敛到同一行，成员不重复挂
        resource2 = await _seed_resource(db_session, channel, search_title="某大IP")
        await link_franchise_pack(db_session, resource2, _report("作品A"), channel)
    await db_session.flush()
    assert first_id is not None
    assert resource2.collection_id == first_id


async def test_member_in_other_collection_is_not_stolen(db_session):
    channel = await _seed_channel(db_session)
    other = WorkCollection(
        id=_uuid(), title_cn="别的合集",
        external_source="tmdb_collection", external_id="7",
    )
    db_session.add(other)
    resource = await _seed_resource(db_session, channel, search_title="某大IP")
    agent = _agent_returning()
    # 预置一个已属于其他合集的 TVSeries，标题与 agent 返回一致
    from app.models.series import TVSeries
    preowned = TVSeries(
        id=_uuid(), title_cn="作品A", title_en="作品A",
        original_title="作品A", content_type="tv",
        collection_id=other.id,
    )
    db_session.add(preowned)
    await db_session.flush()

    with _patch_agent(agent), patch(_POSTER, new_callable=AsyncMock, return_value=None):
        await link_franchise_pack(db_session, resource, _report("作品A"), channel)
    await db_session.flush()
    # 作品不被抢走；资源仍链到新建合集（至少一个成员成功即建合集）
    await db_session.refresh(preowned)
    assert preowned.collection_id == other.id
    assert resource.collection_id not in (None, other.id)


# ---------------------------------------------------------------------------
# 失败路径
# ---------------------------------------------------------------------------


async def test_no_work_titles_keeps_batch_verdict_only(db_session):
    channel = await _seed_channel(db_session)
    resource = await _seed_resource(db_session, channel)
    await link_franchise_pack(db_session, resource, _report(), channel)
    assert resource.collection_id is None


async def test_all_members_failed_creates_nothing(db_session):
    channel = await _seed_channel(db_session)
    resource = await _seed_resource(db_session, channel)

    class _NotFound:
        async def process_title_only(self, raw_title, source):
            return SimpleNamespace(found=False, content_type=None,
                                   matched_entity=None)

    with _patch_agent(_NotFound()):
        await link_franchise_pack(db_session, resource, _report("X", "Y"), channel)
    assert resource.collection_id is None


async def test_member_raise_and_untitled_and_non_tv_movie_skipped(db_session):
    """单成员异常/无标题实体/非 tv+movie 内容类型都被跳过。"""
    channel = await _seed_channel(db_session)
    resource = await _seed_resource(db_session, channel, search_title="混合包")

    class _Mixed:
        async def process_title_only(self, raw_title, source):
            if "爆" in raw_title:
                raise RuntimeError("boom")
            if "哑" in raw_title:
                return SimpleNamespace(
                    found=True, content_type="tv",
                    matched_entity={"external_id": "x"},  # 无任何标题键
                )
            if "广播" in raw_title:
                return SimpleNamespace(
                    found=True, content_type="drama_cd",
                    matched_entity={"content_type": "drama_cd",
                                    "title_cn": "广播剧"},
                )
            return SimpleNamespace(
                found=True, content_type="movie",
                matched_entity={
                    "content_type": "movie", "external_id": "m1",
                    "external_source": "llm_search",
                    "title_cn": raw_title, "title_en": raw_title,
                },
            )

    with _patch_agent(_Mixed()), patch(_POSTER, new_callable=AsyncMock, return_value=None):
        await link_franchise_pack(
            db_session, resource,
            _report("爆炸成员", "哑成员", "广播剧成员", "正常剧场版"),
            channel,
        )
    await db_session.flush()
    # 只有 movie 成员解析成功 → 建合集并挂载
    assert resource.collection_id is not None


async def test_agent_exception_on_process_title_is_absorbed(db_session):
    channel = await _seed_channel(db_session)
    resource = await _seed_resource(db_session, channel)

    class _Boom:
        async def process_title_only(self, raw_title, source):
            raise RuntimeError("agent down")

    with _patch_agent(_Boom()):
        await link_franchise_pack(db_session, resource, _report("Z"), channel)
    assert resource.collection_id is None


# ---------------------------------------------------------------------------
# get-or-create 归一化与 _pack_title 回退链
# ---------------------------------------------------------------------------


async def test_collection_get_or_create_matches_normalized_title(db_session):
    channel = await _seed_channel(db_session)
    existing = WorkCollection(
        id=_uuid(), title_cn="某大IP！",
        external_source=FRANCHISE_PACK_SOURCE, external_id=None,
    )
    db_session.add(existing)
    resource = await _seed_resource(db_session, channel, search_title="某大ip！")
    with _patch_agent(_agent_returning()), patch(_POSTER, new_callable=AsyncMock, return_value=None):
        await link_franchise_pack(db_session, resource, _report("作品A"), channel)
    await db_session.flush()
    assert resource.collection_id == existing.id


def test_pack_title_fallback_chain():
    r = SimpleNamespace(search_title=None, title_cn=None,
                        title_raw="[整理搬运] 猫眼三姐妹／猫之眼：TV+剧场版")
    assert _pack_title(r) == "猫眼三姐妹"
    r2 = SimpleNamespace(search_title=None, title_cn=None, title_raw="")
    assert _pack_title(r2) == "franchise pack"
    r3 = SimpleNamespace(search_title="  [Group] Title  ", title_cn=None,
                         title_raw="x")
    assert _pack_title(r3) == "Title"
