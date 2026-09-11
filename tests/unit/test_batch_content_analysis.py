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


# ---------------------------------------------------------------------------
# Deterministic helpers / pure functions
# ---------------------------------------------------------------------------

from types import SimpleNamespace as _SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import batch_content_analysis as _bca
from app.services.torrent_inspect import TorrentReport, WorkCluster


def test_llm_refinement_needed_gates(monkeypatch):
    cfg = _SimpleNamespace(
        llm_api_key="k", llm_base_url="", llm_model="", llm_enable_thinking=False,
    )
    monkeypatch.setattr(_bca, "runtime_config", cfg)
    assert _bca.llm_refinement_needed(
        TorrentReport(scope="franchise", is_batch=True), "franchise"
    ) is True
    assert _bca.llm_refinement_needed(
        TorrentReport(
            scope="season", is_batch=True, video_file_count=2, unparsed_ratio=0.6,
        ),
        "season",
    ) is True
    assert _bca.llm_refinement_needed(
        TorrentReport(
            scope="season", is_batch=True, video_file_count=2, unparsed_ratio=0.3,
        ),
        "season",
    ) is False
    assert _bca.llm_refinement_needed(
        TorrentReport(
            scope="season", is_batch=False, video_file_count=2, unparsed_ratio=0.6,
        ),
        "season",
    ) is False
    # no api key -> never fires
    monkeypatch.setattr(
        _bca, "runtime_config",
        _SimpleNamespace(llm_api_key="", llm_base_url="", llm_model="",
                         llm_enable_thinking=False),
    )
    assert _bca.llm_refinement_needed(
        TorrentReport(scope="franchise", is_batch=True), "franchise"
    ) is False


def test_build_listing_text_and_parse_llm_json():
    text = _bca._build_listing_text([
        {"name": "a.mkv", "size": 10 * 1024 * 1024},
        {"name": "b.mkv", "size": None},
    ])
    assert "a.mkv (10MB)" in text
    assert "b.mkv (0MB)" in text
    assert _bca._parse_llm_json('```json\n{"works": []}\n```') == {"works": []}
    assert _bca._parse_llm_json('prefix {"works": []} suffix') == {"works": []}
    with pytest.raises(ValueError):
        _bca._parse_llm_json("not json at all")


def test_valid_paths_clamps_llm_output():
    known = {"a.mkv", "b.mkv", "c.mkv", "d.mkv", "e.mkv", "f.mkv"}
    cands = [{
        "candidate_key": "series:1", "work_type": "series", "work_id": "1",
        "titles": ["候选剧"],
    }]
    data = {
        "works": [
            "junk",
            {
                "candidate_key": "series:1", "title": "", "content_type": "movie",
                "files": [{"path": "a.mkv", "season": 1, "episode": 1}],
            },
            {
                "candidate_key": "made:up", "title": "x", "content_type": "tv",
                "files": [{"path": "b.mkv"}],
            },
            {
                "candidate_key": None, "title": "   ", "content_type": "tv",
                "files": [{"path": "c.mkv"}],
            },
            {
                "candidate_key": None, "title": "film", "content_type": "music",
                "files": [{"path": "d.mkv"}],
            },
            {
                "candidate_key": None, "title": "剧B", "content_type": "tv",
                "files": [
                    "notdict",
                    {"path": "z.mkv", "episode": 3},
                    {"path": "e.mkv"},
                ],
            },
            {
                "candidate_key": None, "title": "剧C", "content_type": "movie",
                "files": [{"path": "f.mkv", "episode_start": 2, "episode_end": 1}],
            },
            {
                "candidate_key": None, "title": "空", "content_type": "movie",
                "files": [],
            },
        ]
    }
    out = _bca._valid_paths(data, known, cands)
    by_title = {w["title"]: w for w in out}
    assert set(by_title) == {"候选剧", "剧B", "剧C"}
    a = by_title["候选剧"]
    assert a["content_type"] == "tv"  # candidate work_type wins over LLM ctype
    assert a["files"] == [{"path": "a.mkv", "season": 1, "episode_start": 1, "episode_end": 1}]
    assert by_title["剧B"]["files"] == [
        {"path": "e.mkv", "season": None, "episode_start": None, "episode_end": None},
    ]
    assert by_title["剧C"]["files"] == [
        {"path": "f.mkv", "season": None, "episode_start": None, "episode_end": None},
    ]


# ---------------------------------------------------------------------------
# Assignment write-backs
# ---------------------------------------------------------------------------


async def test_apply_auto_assignments_upserts_and_prunes(db_session, sample_channel):
    from app.models.file_resource import FileResource
    from app.models.resource_file_assignment import ResourceFileAssignment

    resource = FileResource(
        id=_uuid(), channel_id=sample_channel.id, guid=_uuid(),
        title_raw="pack", torrent_url="https://x/a.torrent",
        is_batch=True, batch_scope="season",
    )
    db_session.add(resource)
    db_session.add_all([
        ResourceFileAssignment(
            id=_uuid(), resource_id=resource.id, file_path="Show/E01.mkv", source="auto",
        ),
        ResourceFileAssignment(
            id=_uuid(), resource_id=resource.id, file_path="Show/E02.mkv", source="llm",
        ),
        ResourceFileAssignment(
            id=_uuid(), resource_id=resource.id, file_path="old/Stale.mkv", source="auto",
        ),
    ])
    await db_session.flush()
    await db_session.refresh(resource, ["file_assignments"])

    report = TorrentReport(
        scope="season", is_batch=True,
        clusters=[WorkCluster(
            title="Show", files=["Show/E01.mkv", "Show/E02.mkv", "Show/E03.mkv"],
        )],
        file_parses=[
            {"path": "Show/E01.mkv", "size": 500_000_000, "season": 1, "episode": 1},
            {"path": "Show/E02.mkv", "size": 500_000_000, "season": 1, "episode": 2},
            {"path": "Show/E03.mkv", "size": 500_000_000, "season": 1, "episode": 3},
        ],
    )
    _bca.apply_auto_assignments(resource, report)
    await db_session.flush()

    rows = (await db_session.execute(
        select(ResourceFileAssignment).where(ResourceFileAssignment.resource_id == resource.id)
    )).scalars().all()
    by_path = {r.file_path: r for r in rows}
    assert set(by_path) == {"Show/E01.mkv", "Show/E02.mkv", "Show/E03.mkv"}
    e01 = by_path["Show/E01.mkv"]
    assert e01.source == "auto"
    assert e01.episode_start == 1 and e01.episode_end == 1
    assert e01.season == 1
    assert e01.work_title_hint == "Show"
    assert e01.file_size == 500_000_000
    assert by_path["Show/E02.mkv"].source == "llm"  # llm provenance untouched
    assert by_path["Show/E02.mkv"].season is None
    e03 = by_path["Show/E03.mkv"]
    assert e03.source == "auto"
    assert e03.episode_start == 3 and e03.episode_end == 3


async def test_apply_auto_assignments_filename_group_heuristic(db_session, sample_channel):
    from app.models.file_resource import FileResource

    resource = FileResource(
        id=_uuid(), channel_id=sample_channel.id, guid=_uuid(),
        title_raw="pack", torrent_url="https://x/a.torrent",
        is_batch=True, batch_scope="season",
    )
    db_session.add(resource)
    await db_session.flush()
    await db_session.refresh(resource, ["file_assignments"])

    report = TorrentReport(
        scope="season", is_batch=True,
        file_parses=[
            {"path": "Show/E01-GRP.mkv", "size": 1, "season": 1, "episode": 1},
            {"path": "Show/E02-GRP.mkv", "size": 1, "season": 1, "episode": 2},
        ],
    )
    _bca.apply_auto_assignments(resource, report)

    assert resource.subtitle_groups == ["GRP"]
    assert resource.subtitle_group == "GRP"
    assert resource.subtitle_groups_source == "heuristic"


async def test_apply_auto_assignments_two_filename_groups_not_stamped(
    db_session, sample_channel,
):
    from app.models.file_resource import FileResource

    resource = FileResource(
        id=_uuid(), channel_id=sample_channel.id, guid=_uuid(),
        title_raw="pack", torrent_url="https://x/a.torrent",
        is_batch=True, batch_scope="season",
    )
    db_session.add(resource)
    await db_session.flush()
    await db_session.refresh(resource, ["file_assignments"])

    report = TorrentReport(
        scope="season", is_batch=True,
        file_parses=[
            {"path": "Show/E01-AAA.mkv", "size": 1, "season": 1, "episode": 1},
            {"path": "Show/E02-BBB.mkv", "size": 1, "season": 1, "episode": 2},
        ],
    )
    _bca.apply_auto_assignments(resource, report)

    assert resource.subtitle_group is None
    assert resource.subtitle_groups is None


async def test_apply_auto_assignments_empty_report_noop(db_session, sample_channel):
    from app.models.file_resource import FileResource
    from app.models.resource_file_assignment import ResourceFileAssignment

    resource = FileResource(
        id=_uuid(), channel_id=sample_channel.id, guid=_uuid(),
        title_raw="pack", torrent_url="https://x/a.torrent",
        is_batch=True, batch_scope="season",
    )
    db_session.add(resource)
    await db_session.flush()

    _bca.apply_auto_assignments(resource, TorrentReport(scope="single", is_batch=False))
    await db_session.flush()

    assert (await db_session.execute(select(ResourceFileAssignment))).scalars().all() == []


async def test_compute_season_ranges(db_session, sample_channel):
    from app.models.file_resource import FileResource
    from app.models.resource_file_assignment import ResourceFileAssignment

    resource = FileResource(
        id=_uuid(), channel_id=sample_channel.id, guid=_uuid(),
        title_raw="pack", torrent_url="https://x/a.torrent",
        is_batch=True, batch_scope="season",
    )
    db_session.add(resource)
    await db_session.flush()
    db_session.add_all([
        ResourceFileAssignment(
            id=_uuid(), resource_id=resource.id, file_path="a.mkv",
            season=1, episode_start=1, episode_end=1, source="auto",
        ),
        ResourceFileAssignment(
            id=_uuid(), resource_id=resource.id, file_path="b.mkv",
            season=1, episode_start=2, episode_end=2, source="auto",
        ),
        ResourceFileAssignment(
            id=_uuid(), resource_id=resource.id, file_path="c.mkv",
            season=None, episode_start=1, episode_end=1, source="auto",
        ),
        ResourceFileAssignment(
            id=_uuid(), resource_id=resource.id, file_path="d.mkv",
            season=2, episode_start=None, episode_end=None, source="auto",
        ),
    ])
    await db_session.flush()
    await db_session.refresh(resource, ["file_assignments"])

    ranges = _bca.compute_season_ranges(resource)
    assert ranges == [{"season": 1, "episode_start": 1, "episode_end": 2}]

    empty = FileResource(
        id=_uuid(), channel_id=sample_channel.id, guid=_uuid(),
        title_raw="empty", torrent_url="https://x/b.torrent",
    )
    db_session.add(empty)
    await db_session.flush()
    await db_session.refresh(empty, ["file_assignments"])
    assert _bca.compute_season_ranges(empty) is None


# ---------------------------------------------------------------------------
# resolve_fractional_specials
# ---------------------------------------------------------------------------


async def test_resolve_fractional_specials_mapping(db_session):
    from app.models.episode import Episode
    from app.models.series import TVSeries

    series = TVSeries(id=_uuid(), title_cn="作品")
    db_session.add(series)
    await db_session.flush()
    db_session.add_all([
        Episode(id=_uuid(), series_id=series.id, season=0, episode=1),
        Episode(id=_uuid(), series_id=series.id, season=0, episode=2),
    ])
    await db_session.flush()

    paths = ["Pack/11.5.mkv", "Pack/22.5.mkv"]
    mapping = await _bca.resolve_fractional_specials(db_session, series.id, paths)
    assert mapping == {"Pack/11.5.mkv": 1, "Pack/22.5.mkv": 2}

    # no season-0 rows -> canonical sequential assignment
    bare = TVSeries(id=_uuid(), title_cn="无特典")
    db_session.add(bare)
    await db_session.flush()
    assert await _bca.resolve_fractional_specials(db_session, bare.id, paths) == {
        "Pack/11.5.mkv": 1, "Pack/22.5.mkv": 2,
    }

    # cardinality mismatch -> no mapping
    few = TVSeries(id=_uuid(), title_cn="特典少")
    db_session.add(few)
    await db_session.flush()
    db_session.add(Episode(id=_uuid(), series_id=few.id, season=0, episode=1))
    await db_session.flush()
    assert await _bca.resolve_fractional_specials(db_session, few.id, paths) == {}

    assert await _bca.resolve_fractional_specials(db_session, series.id, ["a.mkv"]) == {}


# ---------------------------------------------------------------------------
# bind_single_work_assignments / sync_resource_collection
# ---------------------------------------------------------------------------


async def test_bind_single_work_assignments_no_fk_returns_zero(db_session):
    from app.models.file_resource import FileResource

    resource = FileResource(
        id=_uuid(), channel_id="ch", guid=_uuid(), title_raw="x",
        torrent_url="https://x/a.torrent",
    )
    assert await _bca.bind_single_work_assignments(db_session, resource) == 0


async def test_bind_single_work_assignments_refresh_failure_returns_zero():
    class _FakeDB:
        async def refresh(self, *a, **k):
            raise RuntimeError("boom")

    resource = _SimpleNamespace(series_id="s", movie_id=None)
    assert await _bca.bind_single_work_assignments(_FakeDB(), resource) == 0


async def test_bind_single_work_assignments_binds_and_specials(db_session, sample_channel):
    from app.models.episode import Episode
    from app.models.file_resource import FileResource
    from app.models.resource_file_assignment import ResourceFileAssignment
    from app.models.resource_work_link import ResourceWorkLink
    from app.models.series import TVSeries
    from app.models.work_collection import WorkCollection

    collection = WorkCollection(
        id=_uuid(), title_cn="合集", external_source="series_group", external_id=_uuid(),
    )
    work = TVSeries(id=_uuid(), title_cn="作品", collection_id=collection.id)
    other = TVSeries(id=_uuid(), title_cn="其他")
    db_session.add_all([collection, work, other])
    await db_session.flush()
    resource = FileResource(
        id=_uuid(), channel_id=sample_channel.id, guid=_uuid(),
        title_raw="pack", torrent_url="https://x/a.torrent",
        is_batch=True, batch_scope="season", series_id=work.id, season=1,
    )
    db_session.add(resource)
    await db_session.flush()
    db_session.add_all([
        ResourceFileAssignment(
            id=_uuid(), resource_id=resource.id, file_path="Show/E01.mkv", source="auto",
        ),
        ResourceFileAssignment(
            id=_uuid(), resource_id=resource.id, file_path="Show/E02.mkv", source="llm",
        ),
        ResourceFileAssignment(
            id=_uuid(), resource_id=resource.id, file_path="Pack/11.5.mkv", source="auto",
        ),
        ResourceWorkLink(id=_uuid(), resource_id=resource.id, series_id=work.id, source="auto"),
        ResourceWorkLink(id=_uuid(), resource_id=resource.id, series_id=other.id, source="auto"),
        Episode(id=_uuid(), series_id=work.id, season=0, episode=1),
    ])
    await db_session.flush()
    await db_session.refresh(resource, ["file_assignments"])

    changed = await _bca.bind_single_work_assignments(db_session, resource)
    await db_session.commit()

    assert changed == 2
    by_path = {a.file_path: a for a in resource.file_assignments}
    assert by_path["Show/E01.mkv"].series_id == work.id
    assert by_path["Show/E01.mkv"].season == 1
    assert by_path["Show/E02.mkv"].series_id is None  # llm untouched
    assert by_path["Pack/11.5.mkv"].series_id == work.id
    assert by_path["Pack/11.5.mkv"].season == 0
    assert by_path["Pack/11.5.mkv"].episode_start == 1
    assert resource.collection_id == collection.id
    links = (await db_session.execute(
        select(ResourceWorkLink).where(ResourceWorkLink.resource_id == resource.id)
    )).scalars().all()
    assert len(links) == 1
    assert links[0].series_id == work.id


async def test_bind_single_work_assignments_adds_auto_link(db_session, sample_channel):
    from app.models.file_resource import FileResource
    from app.models.resource_file_assignment import ResourceFileAssignment
    from app.models.resource_work_link import ResourceWorkLink
    from app.models.series import TVSeries

    work = TVSeries(id=_uuid(), title_cn="作品")
    other = TVSeries(id=_uuid(), title_cn="其他")
    db_session.add_all([work, other])
    await db_session.flush()
    resource = FileResource(
        id=_uuid(), channel_id=sample_channel.id, guid=_uuid(),
        title_raw="pack", torrent_url="https://x/a.torrent",
        is_batch=True, batch_scope="season", series_id=work.id, season=1,
    )
    db_session.add(resource)
    await db_session.flush()
    db_session.add_all([
        ResourceWorkLink(id=_uuid(), resource_id=resource.id, series_id=other.id, source="auto"),
        ResourceFileAssignment(
            id=_uuid(), resource_id=resource.id, file_path="Show/E01.mkv",
            source="auto", season=1, episode_start=1, episode_end=1,
        ),
    ])
    await db_session.flush()
    await db_session.refresh(resource, ["file_assignments"])

    changed = await _bca.bind_single_work_assignments(db_session, resource)
    await db_session.commit()

    assert changed == 1
    links = (await db_session.execute(
        select(ResourceWorkLink).where(ResourceWorkLink.resource_id == resource.id)
    )).scalars().all()
    assert len(links) == 1
    assert links[0].series_id == work.id
    assert links[0].source == "auto"


async def test_sync_resource_collection_movie_links(db_session, sample_channel):
    from app.models.file_resource import FileResource
    from app.models.movie import Movie
    from app.models.resource_work_link import ResourceWorkLink
    from app.models.work_collection import WorkCollection

    c1 = WorkCollection(
        id=_uuid(), title_cn="合集一", external_source="series_group", external_id=_uuid(),
    )
    c2 = WorkCollection(
        id=_uuid(), title_cn="合集二", external_source="series_group", external_id=_uuid(),
    )
    m1 = Movie(id=_uuid(), title_cn="M1", collection_id=c1.id)
    m2 = Movie(id=_uuid(), title_cn="M2", collection_id=c2.id)
    db_session.add_all([c1, c2, m1, m2])
    await db_session.flush()
    resource = FileResource(
        id=_uuid(), channel_id=sample_channel.id, guid=_uuid(),
        title_raw="跨合集电影包", torrent_url="https://x/cross.torrent",
        is_batch=True, batch_scope="multi_season", movie_id=m1.id,
        collection_id=c1.id,
    )
    db_session.add(resource)
    await db_session.flush()
    db_session.add(ResourceWorkLink(id=_uuid(), resource_id=resource.id, movie_id=m2.id, source="auto"))
    await db_session.flush()

    await _bca.sync_resource_collection(db_session, resource)
    assert resource.collection_id is None


# ---------------------------------------------------------------------------
# build_candidate_works
# ---------------------------------------------------------------------------


async def test_build_candidate_works_collects_titles(db_session, sample_channel):
    from app.models.file_resource import FileResource
    from app.models.movie import Movie
    from app.models.resource_work_link import ResourceWorkLink
    from app.models.series import TVSeries

    s = TVSeries(id=_uuid(), title_cn="剧A", title_en="Show A",
                 original_title="Show A", canonical_name="Show A")
    m = Movie(id=_uuid(), title_cn="电影")
    db_session.add_all([s, m])
    await db_session.flush()
    resource = FileResource(
        id=_uuid(), channel_id=sample_channel.id, guid=_uuid(),
        title_raw="pack", torrent_url="https://x/a.torrent",
        is_batch=True, batch_scope="multi_season", series_id=s.id,
    )
    db_session.add(resource)
    await db_session.flush()
    db_session.add(ResourceWorkLink(id=_uuid(), resource_id=resource.id, movie_id=m.id, source="auto"))
    await db_session.flush()

    cands = await _bca.build_candidate_works(db_session, resource)

    assert len(cands) == 2
    by_key = {c["candidate_key"]: c for c in cands}
    series_key = f"series:{s.id}"
    assert by_key[series_key]["work_type"] == "series"
    assert by_key[series_key]["work_id"] == s.id
    assert by_key[series_key]["titles"] == ["剧A", "Show A"]
    assert by_key[f"movie:{m.id}"]["titles"] == ["电影"]


async def test_build_candidate_works_skips_missing_work():
    class _Result:
        def scalars(self):
            return self

        def all(self):
            return [_SimpleNamespace(series_id="gone", movie_id=None)]

    class _FakeDB:
        async def execute(self, stmt):
            return _Result()

        async def get(self, model, work_id):
            return None

    resource = _SimpleNamespace(id="res", series_id="gone", movie_id="gone2")
    assert await _bca.build_candidate_works(_FakeDB(), resource) == []


# ---------------------------------------------------------------------------
# LLM refinement: analyze_listing / analyze_listing_stream
# ---------------------------------------------------------------------------


def _fake_openai_client(monkeypatch, completions):
    import openai

    class _OpenAI:
        def __init__(self, *a, **k):
            self.chat = _SimpleNamespace(completions=completions)

    monkeypatch.setattr(openai, "AsyncOpenAI", _OpenAI)


def _cfg(monkeypatch, *, api_key="k"):
    monkeypatch.setattr(
        _bca, "runtime_config",
        _SimpleNamespace(
            llm_api_key=api_key, llm_base_url="http://llm",
            llm_model="m", llm_enable_thinking=False,
            llm_extra_body=lambda: {
                "enable_thinking": False,
                "chat_template_kwargs": {"enable_thinking": False},
            },
        ),
    )


class _Completions:
    def __init__(self, content="", exc=None, captured=None):
        self._content = content
        self._exc = exc
        self._captured = captured

    async def create(self, **kwargs):
        if self._captured is not None:
            self._captured.update(kwargs)
        if self._exc:
            raise self._exc
        return _SimpleNamespace(
            choices=[_SimpleNamespace(message=_SimpleNamespace(content=self._content))]
        )


async def test_analyze_listing_no_key_returns_none(monkeypatch):
    _cfg(monkeypatch, api_key="")
    assert await _bca.analyze_listing("t", [{"name": "a.mkv", "size": 1}], []) is None


async def test_analyze_listing_success_and_failure(monkeypatch):
    _cfg(monkeypatch)
    captured = {}
    _fake_openai_client(monkeypatch, _Completions(
        content='{"works": [{"candidate_key": null, "title": "电影", "content_type": "movie", "files": [{"path": "a.mkv", "episode": 1}]}]}',
        captured=captured,
    ))
    data = await _bca.analyze_listing(
        "title", [{"name": "a.mkv", "size": 1}], ["clusterA"],
        [{"candidate_key": "movie:x", "work_type": "movie", "work_id": "x", "titles": ["电影"]}],
    )
    assert data is not None
    assert data["works"][0]["title"] == "电影"
    assert captured["model"] == "m"
    assert captured["temperature"] == 0.1
    assert captured["extra_body"] == {
        "enable_thinking": False,
        "chat_template_kwargs": {"enable_thinking": False},
    }

    # exception in the LLM call -> None
    _fake_openai_client(monkeypatch, _Completions(exc=RuntimeError("boom")))
    assert await _bca.analyze_listing("t", [{"name": "a.mkv", "size": 1}], []) is None

    # response without a works list -> None
    _fake_openai_client(monkeypatch, _Completions(content='{"foo": 1}'))
    assert await _bca.analyze_listing("t", [{"name": "a.mkv", "size": 1}], []) is None


async def test_analyze_listing_stream_events(monkeypatch):
    _cfg(monkeypatch)

    async def _stream(**kwargs):
        for chunk in ['{"works": [', '{"title": "剧", "content_type": "tv"', ", \"files\": []}", "]}"]:
            yield _SimpleNamespace(
                choices=[_SimpleNamespace(delta=_SimpleNamespace(content=chunk))]
            )

    class _StreamCompletions:
        async def create(self, **kwargs):
            return _stream()

    _fake_openai_client(monkeypatch, _StreamCompletions())

    events = [ev async for ev in _bca.analyze_listing_stream(
        "t", [{"name": "a.mkv", "size": 1}], ["锚点A"],
    )]
    assert any(ev[0] == "delta" for ev in events)
    result = [ev[1] for ev in events if ev[0] == "result"]
    assert result == [{"works": [{"title": "剧", "content_type": "tv", "files": []}]}]


async def test_analyze_listing_stream_no_key(monkeypatch):
    _cfg(monkeypatch, api_key="")
    events = [ev async for ev in _bca.analyze_listing_stream("t", [], [])]
    assert events == [("result", None)]


async def test_analyze_listing_stream_errors(monkeypatch):
    _cfg(monkeypatch)

    async def _bad_stream(**kwargs):
        yield _SimpleNamespace(
            choices=[_SimpleNamespace(delta=_SimpleNamespace(content="not json"))]
        )

    class _BadStreamCompletions:
        async def create(self, **kwargs):
            return _bad_stream()

    _fake_openai_client(monkeypatch, _BadStreamCompletions())
    events = [ev async for ev in _bca.analyze_listing_stream("t", [{"name": "a.mkv", "size": 1}], [])]
    assert any(ev[0] == "error" for ev in events)
    assert ("result", None) in events

    # parses but has no works list -> ValueError -> error + result None
    async def _no_works_stream(**kwargs):
        yield _SimpleNamespace(
            choices=[_SimpleNamespace(delta=_SimpleNamespace(content='{"foo": 1}'))]
        )

    class _NoWorksCompletions:
        async def create(self, **kwargs):
            return _no_works_stream()

    _fake_openai_client(monkeypatch, _NoWorksCompletions())
    events = [ev async for ev in _bca.analyze_listing_stream("t", [{"name": "a.mkv", "size": 1}], [])]
    assert any(ev[0] == "error" for ev in events)
    assert ("result", None) in events

    class _ErrStreamCompletions:
        async def create(self, **kwargs):
            raise RuntimeError("stream down")

    _fake_openai_client(monkeypatch, _ErrStreamCompletions())
    events = [ev async for ev in _bca.analyze_listing_stream("t", [{"name": "a.mkv", "size": 1}], [])]
    assert any(ev[0] == "error" for ev in events)
    assert ("result", None) in events


# ---------------------------------------------------------------------------
# refine_batch_content
# ---------------------------------------------------------------------------


async def test_refine_batch_content_empty_and_no_data(db_session, sample_channel, monkeypatch):
    from app.models.file_resource import FileResource

    resource = FileResource(
        id=_uuid(), channel_id=sample_channel.id, guid=_uuid(),
        title_raw="pack", torrent_url="https://x/a.torrent",
        is_batch=True, batch_scope="franchise", search_title="Pack",
    )
    db_session.add(resource)
    await db_session.flush()

    report = TorrentReport(scope="franchise", is_batch=True)
    assert await _bca.refine_batch_content(db_session, resource, report, sample_channel) is False

    monkeypatch.setattr(_bca, "analyze_listing", AsyncMock(return_value=None))
    report2 = TorrentReport(
        scope="franchise", is_batch=True,
        file_parses=[{"path": "a.mkv", "size": 1}],
    )
    assert await _bca.refine_batch_content(db_session, resource, report2, sample_channel) is False

    monkeypatch.setattr(
        _bca, "analyze_listing",
        AsyncMock(return_value={
            "works": [
                {"candidate_key": None, "title": "  ", "content_type": "tv",
                 "files": [{"path": "a.mkv"}]},
            ],
        }),
    )
    assert await _bca.refine_batch_content(db_session, resource, report2, sample_channel) is False


async def test_refine_batch_content_binds_movies_and_upgrades_scope(
    db_session, sample_channel, monkeypatch,
):
    from app.models.file_resource import FileResource
    from app.models.movie import Movie
    from app.models.resource_file_assignment import ResourceFileAssignment
    from app.models.resource_work_link import ResourceWorkLink

    resource = FileResource(
        id=_uuid(), channel_id=sample_channel.id, guid=_uuid(),
        title_raw="MoviePack", torrent_url="https://x/a.torrent",
        is_batch=True, batch_scope="franchise", search_title="MoviePack",
    )
    db_session.add(resource)
    await db_session.flush()
    db_session.add_all([
        ResourceFileAssignment(
            id=_uuid(), resource_id=resource.id, file_path="FilmA/One.mkv", source="auto",
        ),
        ResourceFileAssignment(
            id=_uuid(), resource_id=resource.id, file_path="FilmB/Two.mkv", source="auto",
        ),
    ])
    await db_session.flush()
    await db_session.refresh(resource, ["file_assignments"])

    movie = Movie(id=_uuid(), title_cn="电影一")
    db_session.add(movie)
    await db_session.flush()

    report = TorrentReport(
        scope="franchise", is_batch=True,
        file_parses=[
            {"path": "FilmA/One.mkv", "size": 10},
            {"path": "FilmB/Two.mkv", "size": 10},
        ],
        clusters=[
            WorkCluster(title="FilmA", files=["FilmA/One.mkv"]),
            WorkCluster(title="FilmB", files=["FilmB/Two.mkv"]),
        ],
    )
    monkeypatch.setattr(
        _bca, "analyze_listing",
        AsyncMock(return_value={
            "works": [
                {"candidate_key": None, "title": "电影一", "content_type": "movie",
                 "files": [{"path": "FilmA/One.mkv"}]},
                {"candidate_key": None, "title": "电影二", "content_type": "movie",
                 "files": [{"path": "FilmB/Two.mkv"}]},
            ],
        }),
    )

    async def _resolve_movie(db, resource, channel, title):
        return movie if title == "电影一" else None

    monkeypatch.setattr(_bca, "_resolve_movie", _resolve_movie)

    bound = await _bca.refine_batch_content(db_session, resource, report, sample_channel)
    await db_session.flush()

    assert bound is True
    assert resource.batch_scope == "movies"
    by_path = {a.file_path: a for a in resource.file_assignments}
    assert by_path["FilmA/One.mkv"].movie_id == movie.id
    assert by_path["FilmA/One.mkv"].source == "llm"
    # unresolvable movie cluster degrades to a hint on the unbound row
    assert by_path["FilmB/Two.mkv"].movie_id is None
    assert by_path["FilmB/Two.mkv"].work_title_hint == "电影二"
    assert by_path["FilmB/Two.mkv"].source == "auto"
    links = (await db_session.execute(select(ResourceWorkLink))).scalars().all()
    assert len(links) == 1
    assert links[0].movie_id == movie.id
    assert links[0].source == "llm"

    # second movie bind short-circuits on the existing link
    await _bca._bind_work(db_session, resource, "movie", movie.id, [
        {"path": "FilmA/One.mkv"},
    ])
    assert len((await db_session.execute(select(ResourceWorkLink))).scalars().all()) == 1


async def test_refine_batch_content_tv_hints_only(db_session, sample_channel, monkeypatch):
    from app.models.file_resource import FileResource
    from app.models.resource_file_assignment import ResourceFileAssignment
    from app.models.series import TVSeries

    resource = FileResource(
        id=_uuid(), channel_id=sample_channel.id, guid=_uuid(),
        title_raw="Pack", torrent_url="https://x/a.torrent",
        is_batch=True, batch_scope="season", search_title="Pack",
    )
    db_session.add(resource)
    await db_session.flush()
    s = TVSeries(id=_uuid(), title_cn="某剧")
    db_session.add(s)
    await db_session.flush()
    db_session.add_all([
        ResourceFileAssignment(
            id=_uuid(), resource_id=resource.id, file_path="Show/E01.mkv", source="manual",
        ),
        ResourceFileAssignment(
            id=_uuid(), resource_id=resource.id, file_path="Show/E02.mkv",
            source="auto", series_id=s.id,
        ),
        ResourceFileAssignment(
            id=_uuid(), resource_id=resource.id, file_path="Show/E03.mkv", source="auto",
        ),
    ])
    await db_session.flush()
    await db_session.refresh(resource, ["file_assignments"])

    report = TorrentReport(
        scope="season", is_batch=True,
        file_parses=[
            {"path": "Show/E01.mkv", "size": 1},
            {"path": "Show/E02.mkv", "size": 1},
            {"path": "Show/E03.mkv", "size": 1},
            {"path": "FilmB/Two.mkv", "size": 1},
        ],
    )
    monkeypatch.setattr(
        _bca, "analyze_listing",
        AsyncMock(return_value={
            "works": [
                {"candidate_key": None, "title": "某剧", "content_type": "tv", "files": [
                    {"path": "Show/E01.mkv", "season": 1, "episode_start": 1, "episode_end": 1},
                    {"path": "Show/E02.mkv", "season": 1, "episode_start": 2, "episode_end": 2},
                    {"path": "Show/E03.mkv", "season": 1, "episode_start": 3, "episode_end": 3},
                ]},
                {"candidate_key": None, "title": "电影二", "content_type": "movie",
                 "files": [{"path": "FilmB/Two.mkv"}]},
            ],
        }),
    )

    bound = await _bca.refine_batch_content(db_session, resource, report, sample_channel)
    await db_session.flush()

    assert bound is False
    by_path = {a.file_path: a for a in resource.file_assignments}
    assert by_path["Show/E01.mkv"].source == "manual"  # untouched
    assert by_path["Show/E01.mkv"].work_title_hint is None
    assert by_path["Show/E02.mkv"].series_id == s.id  # bound row untouched
    assert by_path["Show/E02.mkv"].work_title_hint is None
    assert by_path["Show/E03.mkv"].work_title_hint == "某剧"
    assert by_path["Show/E03.mkv"].season == 1
    assert by_path["Show/E03.mkv"].episode_start == 3
    assert by_path["Show/E03.mkv"].episode_end == 3


# ---------------------------------------------------------------------------
# _resolve_movie
# ---------------------------------------------------------------------------


async def test_resolve_movie_success(db_session, monkeypatch):
    from app.models.movie import Movie

    movie = Movie(id=_uuid(), title_cn="电影")
    db_session.add(movie)
    await db_session.flush()

    agent = _SimpleNamespace()
    agent.process_title_only = AsyncMock(return_value=_SimpleNamespace(
        found=True, matched_entity={"title": "电影"}, content_type="movie",
    ))
    monkeypatch.setattr("app.services.metadata_agent.get_agent", lambda: agent)

    async def _upsert(db, entity):
        return movie

    monkeypatch.setattr(
        "app.services.metadata_service.create_or_update_movie_from_external", _upsert,
    )

    got = await _bca._resolve_movie(db_session, _SimpleNamespace(), None, "电影")
    assert got is movie


async def test_resolve_movie_failure_paths(db_session, monkeypatch):
    def _mk_agent(exc=None, found=True, entity=None, ctype="movie"):
        async def _process(title, source):
            if exc:
                raise exc
            return _SimpleNamespace(found=found, matched_entity=entity, content_type=ctype)

        return _SimpleNamespace(process_title_only=_process)

    monkeypatch.setattr(
        "app.services.metadata_agent.get_agent",
        lambda: _mk_agent(exc=RuntimeError("boom")),
    )
    assert await _bca._resolve_movie(db_session, _SimpleNamespace(), None, "t") is None

    monkeypatch.setattr(
        "app.services.metadata_agent.get_agent",
        lambda: _mk_agent(found=False, entity=None),
    )
    assert await _bca._resolve_movie(db_session, _SimpleNamespace(), None, "t") is None

    monkeypatch.setattr(
        "app.services.metadata_agent.get_agent",
        lambda: _mk_agent(found=True, entity={"title": "x"}, ctype="tv"),
    )
    assert await _bca._resolve_movie(db_session, _SimpleNamespace(), None, "t") is None

    async def _upsert_err(db, entity):
        raise RuntimeError("upsert")

    monkeypatch.setattr(
        "app.services.metadata_service.create_or_update_movie_from_external", _upsert_err,
    )
    monkeypatch.setattr(
        "app.services.metadata_agent.get_agent",
        lambda: _mk_agent(found=True, entity={"title": "x"}, ctype="movie"),
    )
    assert await _bca._resolve_movie(db_session, _SimpleNamespace(), None, "t") is None


# ---------------------------------------------------------------------------
# _bind_work
# ---------------------------------------------------------------------------


async def test_bind_work_series_branch_and_link_dedup(db_session, sample_channel):
    from app.models.file_resource import FileResource
    from app.models.resource_file_assignment import ResourceFileAssignment
    from app.models.resource_work_link import ResourceWorkLink
    from app.models.series import TVSeries

    work = TVSeries(id=_uuid(), title_cn="作品")
    db_session.add(work)
    await db_session.flush()
    resource = FileResource(
        id=_uuid(), channel_id=sample_channel.id, guid=_uuid(),
        title_raw="x", torrent_url="https://x/a.torrent",
        is_batch=True, batch_scope="multi_season",
    )
    db_session.add(resource)
    await db_session.flush()
    db_session.add_all([
        ResourceFileAssignment(
            id=_uuid(), resource_id=resource.id, file_path="Show/E01.mkv", source="auto",
        ),
        ResourceFileAssignment(
            id=_uuid(), resource_id=resource.id, file_path="Show/E02.mkv", source="manual",
        ),
    ])
    await db_session.flush()
    await db_session.refresh(resource, ["file_assignments"])

    await _bca._bind_work(db_session, resource, "series", work.id, [
        {"path": "Show/E01.mkv", "season": 1, "episode_start": 2, "episode_end": 3},
        {"path": "Show/E02.mkv", "season": 1, "episode_start": 1, "episode_end": 1},
        {"path": "Show/E03.mkv", "season": 1, "episode_start": 4, "episode_end": 4},
    ])
    await db_session.flush()

    by_path = {a.file_path: a for a in resource.file_assignments}
    a1 = by_path["Show/E01.mkv"]
    assert a1.series_id == work.id
    assert a1.movie_id is None
    assert a1.season == 1
    assert a1.episode_start == 2 and a1.episode_end == 3
    assert a1.source == "llm"
    assert by_path["Show/E02.mkv"].series_id is None  # manual untouched
    assert by_path["Show/E02.mkv"].source == "manual"

    links = (await db_session.execute(select(ResourceWorkLink))).scalars().all()
    assert len(links) == 1
    assert links[0].series_id == work.id
    assert links[0].source == "llm"

    # second bind short-circuits on the existing link
    await _bca._bind_work(db_session, resource, "series", work.id, [
        {"path": "Show/E01.mkv"},
    ])
    assert len((await db_session.execute(select(ResourceWorkLink))).scalars().all()) == 1


# ---------------------------------------------------------------------------
# suggest_batch_content
# ---------------------------------------------------------------------------


async def test_suggest_batch_content_with_files(monkeypatch):
    report = TorrentReport(
        scope="season", is_batch=True,
        file_parses=[{"path": "Show/E01.mkv", "size": 1, "season": 1, "episode": 1}],
        clusters=[WorkCluster(title="Show", files=["Show/E01.mkv"])],
    )
    monkeypatch.setattr("app.services.torrent_inspect.analyze_torrent_files", lambda files: report)
    monkeypatch.setattr(
        _bca, "analyze_listing",
        AsyncMock(return_value={
            "works": [
                {"candidate_key": None, "title": "Show", "content_type": "tv",
                 "files": [{"path": "Show/E01.mkv", "season": 1, "episode": 1}]},
            ],
        }),
    )

    resource = _SimpleNamespace(
        torrent_file="ignored", search_title="Show", title_cn=None, title_raw="raw",
    )
    result = await _bca.suggest_batch_content(
        None, resource, None, files=[{"name": "Show/E01.mkv", "size": 1}],
    )

    assert result["deterministic"]["scope_hint"] == "season"
    assert result["deterministic"]["seasons"] == []
    assert result["deterministic"]["files"][0]["episode"] == 1
    assert result["deterministic"]["clusters"][0]["title"] == "Show"
    assert result["works"][0]["title"] == "Show"


async def test_suggest_batch_content_no_torrent_file(monkeypatch):
    report = TorrentReport(scope="unknown", is_batch=False)
    monkeypatch.setattr("app.services.torrent_inspect.analyze_torrent_files", lambda files: report)

    resource = _SimpleNamespace(
        torrent_file=None, search_title=None, title_cn=None, title_raw="raw",
    )
    result = await _bca.suggest_batch_content(None, resource)

    assert result["deterministic"]["scope_hint"] == "unknown"
    assert result["works"] == []


async def test_suggest_batch_content_parse_failure(monkeypatch):
    def _boom(path):
        raise RuntimeError("bad torrent")

    monkeypatch.setattr("app.services.torrent_inspect.parse_torrent_files", _boom)
    report = TorrentReport(scope="unknown", is_batch=False)
    monkeypatch.setattr("app.services.torrent_inspect.analyze_torrent_files", lambda files: report)

    resource = _SimpleNamespace(
        torrent_file="x.torrent", search_title=None, title_cn=None, title_raw="raw",
    )
    result = await _bca.suggest_batch_content(None, resource)

    assert result["deterministic"]["scope_hint"] == "unknown"
