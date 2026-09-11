"""``batch_content_analysis`` 绑定与合集沉淀的单元测试。

覆盖 ``bind_single_work_assignments`` 的孤儿 auto link 清理（FK 重指向后
指向其他作品的 auto link 删除、manual 保留）与 ``sync_resource_collection``
的合集身份派生（一致写入 / 不一致置空 / 无关联作品不动）。
"""

from __future__ import annotations

import uuid

from sqlalchemy import select


def _uuid() -> str:
    return str(uuid.uuid4())


async def test_bind_cleans_orphan_auto_links_keeps_manual(db_session, sample_channel):
    """FK 指向 B：指向 A 的 auto link 被删除，manual link 保留。"""
    from app.models.file_resource import FileResource
    from app.models.resource_file_assignment import ResourceFileAssignment
    from app.models.resource_work_link import ResourceWorkLink
    from app.models.series import TVSeries
    from app.models.work_collection import WorkCollection
    from app.services.batch_content_analysis import bind_single_work_assignments

    collection = WorkCollection(
        id=_uuid(), title_cn="合集", external_source="series_group",
        external_id=_uuid(),
    )
    old = TVSeries(id=_uuid(), title_cn="旧作品")
    kept = TVSeries(
        id=_uuid(), title_cn="人工关联作品", collection_id=collection.id,
    )
    work = TVSeries(id=_uuid(), title_cn="新作品", collection_id=collection.id)
    resource = FileResource(
        id=_uuid(), channel_id=sample_channel.id, guid=_uuid(),
        title_raw="重指向合集包", torrent_url="https://x/pack.torrent",
        is_batch=True, batch_scope="season", series_id=work.id,
    )
    # 映射已绑定到新作品（changed=0 路径也要清理孤儿 link）
    assignment = ResourceFileAssignment(
        resource_id=resource.id, file_path="e01.mkv", series_id=work.id,
        season=1, episode_start=1, episode_end=1, source="auto",
    )
    orphan = ResourceWorkLink(
        resource_id=resource.id, series_id=old.id, source="auto",
    )
    manual = ResourceWorkLink(
        resource_id=resource.id, series_id=kept.id, source="manual",
    )
    db_session.add_all([collection, old, kept, work, resource, assignment,
                        orphan, manual])
    await db_session.commit()

    assert await bind_single_work_assignments(db_session, resource) == 0
    await db_session.commit()

    links = (await db_session.execute(
        select(ResourceWorkLink).where(ResourceWorkLink.resource_id == resource.id)
    )).scalars().all()
    # 孤儿 auto link 删除；manual 永不被动
    assert [(link.series_id, link.source) for link in links] == [
        (kept.id, "manual"),
    ]
    # 合集身份经绑定作品沉淀到资源（保留的 manual 作品同属该合集）
    assert resource.collection_id == collection.id


async def test_sync_resource_collection_settles_and_clears(db_session, sample_channel):
    """作品合集一致 → 写入；跨合集 → 置空。"""
    from app.models.file_resource import FileResource
    from app.models.resource_work_link import ResourceWorkLink
    from app.models.series import TVSeries
    from app.models.work_collection import WorkCollection
    from app.services.batch_content_analysis import sync_resource_collection

    c1 = WorkCollection(
        id=_uuid(), title_cn="合集一", external_source="series_group",
        external_id=_uuid(),
    )
    c2 = WorkCollection(
        id=_uuid(), title_cn="合集二", external_source="series_group",
        external_id=_uuid(),
    )
    s1 = TVSeries(id=_uuid(), title_cn="S1", collection_id=c1.id)
    s2 = TVSeries(id=_uuid(), title_cn="S2", collection_id=c2.id)
    resource = FileResource(
        id=_uuid(), channel_id=sample_channel.id, guid=_uuid(),
        title_raw="跨合集包", torrent_url="https://x/cross.torrent",
        is_batch=True, batch_scope="multi_season", series_id=s1.id,
        collection_id=c1.id,
    )
    db_session.add_all([c1, c2, s1, s2, resource])
    await db_session.commit()

    # 一致（仅 FK 作品）→ 维持/写入
    await sync_resource_collection(db_session, resource)
    assert resource.collection_id == c1.id

    # link 指向另一合集作品 → 不一致 → 置空
    db_session.add(ResourceWorkLink(
        resource_id=resource.id, series_id=s2.id, source="auto",
    ))
    await db_session.flush()
    await sync_resource_collection(db_session, resource)
    assert resource.collection_id is None


async def test_sync_resource_collection_without_works_untouched(
    db_session, sample_channel,
):
    """无任何关联作品 → 不动 collection_id（保留 park 状态）。"""
    from app.models.file_resource import FileResource
    from app.models.work_collection import WorkCollection
    from app.services.batch_content_analysis import sync_resource_collection

    collection = WorkCollection(
        id=_uuid(), title_cn="park 合集", external_source="series_group",
        external_id=_uuid(),
    )
    resource = FileResource(
        id=_uuid(), channel_id=sample_channel.id, guid=_uuid(),
        title_raw="parked", torrent_url="https://x/parked.torrent",
        is_batch=True, batch_scope="season", collection_id=collection.id,
    )
    db_session.add_all([collection, resource])
    await db_session.commit()

    await sync_resource_collection(db_session, resource)
    assert resource.collection_id == collection.id

    # franchise/movies 包不碰（即便有关联作品）
    from app.models.series import TVSeries

    work = TVSeries(id=_uuid(), title_cn="franchise 作品")
    resource.batch_scope = "franchise"
    resource.collection_id = None
    db_session.add(work)
    await db_session.flush()
    resource.series_id = work.id
    await sync_resource_collection(db_session, resource)
    assert resource.collection_id is None


async def test_analyze_listing_timeout_and_thinking_body(monkeypatch):
    """F6/F7: analyze_listing sends enable_thinking in both spellings and
    budgets 120s for large franchise-pack listings."""
    from types import SimpleNamespace

    from app.services import batch_content_analysis as bca
    from app.services import runtime_config as rc

    monkeypatch.setitem(rc._overrides, "llm_api_key", "test-key")
    monkeypatch.setitem(rc._overrides, "llm_enable_thinking", "false")

    captured = {}

    class _FakeCompletions:
        async def create(self, **kwargs):
            captured.update(kwargs)
            message = SimpleNamespace(content='{"scope": "franchise", "works": []}')
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    class _FakeClient:
        def __init__(self, **kwargs):
            captured["client_timeout"] = kwargs.get("timeout")

        chat = SimpleNamespace(completions=_FakeCompletions())

    monkeypatch.setattr("openai.AsyncOpenAI", _FakeClient)

    files = [{"name": f"pack/f{i:02d}.mkv", "size": 1000} for i in range(93)]
    result = await bca.analyze_listing("头文字D 全六季", files, ["Initial D First Stage"])

    assert result == {"scope": "franchise", "works": []}
    extra_body = captured["extra_body"]
    assert extra_body["enable_thinking"] is False
    assert extra_body["chat_template_kwargs"] == {"enable_thinking": False}
    assert captured["timeout"] == 120
    # The full 93-file listing is sent, not truncated below the entry cap.
    assert "93 entries" in captured["messages"][1]["content"]
    monkeypatch.delitem(rc._overrides, "llm_api_key", raising=False)
