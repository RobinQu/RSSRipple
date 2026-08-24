"""API tests for the built-in file organization subsystem (organize).

Covers: libraries read/list + partial update (scan-derived, R2: POST removed,
PUT limited to subtitle_lang_map + volume binding repair) + delete guards
(409), organize rules CRUD + save-time validation (DSL / template → 422;
file_op 限 move/hardlink/copy 三值), the dry-run preview endpoint (no persistence), plan list filters &
pagination + pending_reason derivation, execute 409 branches, execute-batch,
classify, cancel, and audit paging.

Execution and media-server refresh are mocked; planning itself is pure and
runs for real against tmp directories.
"""

from __future__ import annotations

import uuid
from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.agent import Agent
from app.models.download_notification import DownloadNotification
from app.models.download_task import DownloadTask
from app.models.file_resource import FileResource
from app.models.library import Library
from app.models.movie import Movie
from app.models.organize_audit import OrganizeAuditEntry
from app.models.organize_plan import OrganizePlan
from app.models.organize_plan_op import OrganizePlanOp
from app.models.organize_rule import OrganizeRule
from app.models.resource_file_assignment import ResourceFileAssignment
from app.models.storage_volume import StorageVolume
from app.utils.time import utcnow


def _uuid() -> str:
    return str(uuid.uuid4())


MOVIE_TEMPLATE = "{category}/{title} ({year})/{title} ({year}){ext}"
TV_TEMPLATE = "{title}/Season {season:02d}/{title} - s{season:02d}e{episode:02d}{ext}"


async def _make_library(db_session, mount_path: str, name="Movies", kind="movie",
                        bound=True):
    """卷绑定形态的 Library（R2）：库根 = volume.mount_path (+ root_subpath)。"""
    lib = Library(id=_uuid(), name=name, kind=kind)
    if bound:
        volume = StorageVolume(
            id=_uuid(), name=f"vol-{lib.id[:8]}", mount_path=mount_path
        )
        db_session.add(volume)
        lib.volume_id = volume.id
    db_session.add(lib)
    await db_session.commit()
    return lib


async def _mkvolume(db_session, mount_path) -> str:
    """建 StorageVolume 并返回 id（直落 DB 的 Library 卷绑定用）。"""
    volume = StorageVolume(
        id=_uuid(), name=f"vol-{_uuid()[:8]}", mount_path=str(mount_path)
    )
    db_session.add(volume)
    await db_session.flush()
    return volume.id


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def library(db_session):
    return await _make_library(db_session, "/media/movies")


@pytest.fixture
async def rule(db_session, library):
    r = OrganizeRule(
        id=_uuid(), name="all-movies", priority=100, enabled=True,
        filter=None, library_id=library.id, path_template=MOVIE_TEMPLATE,
        file_op="move", auto_execute=False,
    )
    db_session.add(r)
    await db_session.commit()
    return r


@pytest.fixture
async def movie_seed(db_session, sample_channel, sample_downloader):
    """Movie + resource + completed task + notification whose payload points
    at a tmp download dir with a single video file."""
    movie = Movie(
        id=_uuid(), title_cn=None, title_en="Test Movie",
        original_title="Test Movie", content_type="movie",
        release_date=date(2020, 1, 1),
    )
    db_session.add(movie)
    await db_session.flush()
    resource = FileResource(
        id=_uuid(), channel_id=sample_channel.id, guid="g1",
        title_raw="Test.Movie.2020.1080p",
        torrent_url="magnet:?xt=urn:btih:abc",
        movie_id=movie.id, is_batch=False,
    )
    agent = Agent(
        id=_uuid(), name="Agent", channel_id=sample_channel.id,
        downloader_id=sample_downloader.id,
    )
    db_session.add_all([resource, agent])
    await db_session.flush()
    task = DownloadTask(
        id=_uuid(), agent_id=agent.id, file_resource_id=resource.id,
        downloader_id=sample_downloader.id, download_dir="/downloads/x",
        transmission_torrent_id=None, status="completed", completed_at=utcnow(),
    )
    db_session.add(task)
    await db_session.flush()
    payload = {
        "notification_id": "n1",
        "agent": {"id": agent.id, "name": agent.name},
        "task": {
            "download_task_id": task.id,
            "download_dir": task.download_dir,
            "torrent_name": "Test.Movie.2020.1080p.mkv",
        },
        "resource": {
            "title_raw": resource.title_raw, "season": None, "episode": None,
            "is_batch": False, "episode_start": None, "episode_end": None,
            "subtitle_langs": None, "resolution": "1080p", "container": "mkv",
            "title_year": 2020,
        },
        "work": {
            "type": "movie", "movie_id": movie.id, "title_en": "Test Movie",
            "title_cn": None, "original_title": "Test Movie", "year": 2020,
            "content_type": "movie", "is_anime": None, "collection": None,
            "genre": None, "seasons": None, "episodes": None,
        },
        "files": [{"name": "Test.Movie.2020.1080p.mkv", "length": 100}],
    }
    notification = DownloadNotification(
        id=_uuid(), agent_id=agent.id, download_task_id=task.id, payload=payload,
    )
    db_session.add(notification)
    await db_session.commit()
    return {
        "movie": movie, "resource": resource, "agent": agent,
        "task": task, "notification": notification, "payload": payload,
    }


@pytest.fixture
def download_file(tmp_path):
    """Materialize the seeded payload's file on disk under a tmp dir."""
    video = tmp_path / "Test.Movie.2020.1080p.mkv"
    video.write_bytes(b"x" * 100)
    return tmp_path


async def _make_plan(db_session, notification, library=None, rule=None, **kw):
    plan = OrganizePlan(
        id=_uuid(), notification_id=notification.id,
        rule_id=rule.id if rule else None,
        library_id=library.id if library else None,
        status=kw.pop("status", "pending"),
        category=kw.pop("category", None),
        payload=notification.payload,
        **kw,
    )
    db_session.add(plan)
    await db_session.flush()
    return plan


# ---------------------------------------------------------------------------
# Libraries
# ---------------------------------------------------------------------------


async def test_library_read_update_delete_roundtrip(client, db_session):
    """Library 为扫描派生（R2）：读/局部更新/删除；root_path 为派生展示。"""
    lib = await _make_library(db_session, "/media/anime", name="TV Anime", kind="tv")

    resp = await client.get("/api/v1/libraries")
    assert resp.status_code == 200
    rows = resp.json()["data"]
    assert len(rows) == 1
    assert rows[0]["pending_plan_count"] == 0
    assert rows[0]["bound"] is True
    assert rows[0]["root_path"] == "/media/anime"  # 派生：卷解析结果

    resp = await client.get(f"/api/v1/libraries/{lib.id}")
    assert resp.status_code == 200
    assert resp.json()["data"]["name"] == "TV Anime"

    resp = await client.put(
        f"/api/v1/libraries/{lib.id}",
        json={"subtitle_lang_map": {"zh-CN": "zh-Hans"}},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["subtitle_lang_map"] == {"zh-CN": "zh-Hans"}

    resp = await client.delete(f"/api/v1/libraries/{lib.id}")
    assert resp.status_code == 200
    assert resp.json()["data"] == {"deleted": True}
    resp = await client.get("/api/v1/libraries")
    assert resp.json()["data"] == []


async def test_library_post_removed(client):
    """手工注册移除（R2）：POST /libraries 不再存在。"""
    resp = await client.post(
        "/api/v1/libraries",
        json={"name": "TV Anime", "root_path": "/media/anime", "kind": "tv"},
    )
    assert resp.status_code == 405


async def test_library_update_rejects_derived_fields(client, library):
    """name/root_path/kind 等字段由扫描派生，提交即 422（extra=forbid）。"""
    for body in ({"name": "x"}, {"root_path": "/media/x"}, {"kind": "tv"},
                 {"plex_section": "3"}):
        resp = await client.put(f"/api/v1/libraries/{library.id}", json=body)
        assert resp.status_code == 422, body


async def test_library_bind_repair_via_put(client, db_session):
    """待绑定 Library 就地修复：PUT volume_id/root_subpath 后 bound。"""
    lib = await _make_library(
        db_session, "/media/unused", name="Unbound", bound=False
    )
    resp = await client.get("/api/v1/libraries?unbound=true")
    rows = resp.json()["data"]
    assert [r["id"] for r in rows] == [lib.id]
    assert rows[0]["bound"] is False and rows[0]["root_path"] is None

    volume = StorageVolume(id=_uuid(), name=f"vol-{_uuid()[:8]}",
                           mount_path="/storage/main")
    db_session.add(volume)
    await db_session.commit()
    resp = await client.put(
        f"/api/v1/libraries/{lib.id}",
        json={"volume_id": volume.id, "root_subpath": "movies"},
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["bound"] is True
    assert data["root_path"] == "/storage/main/movies"

    resp = await client.get("/api/v1/libraries?unbound=true")
    assert resp.json()["data"] == []


async def test_library_put_unknown_volume_404(client, db_session):
    lib = await _make_library(db_session, "/media/x", bound=False)
    resp = await client.put(
        f"/api/v1/libraries/{lib.id}", json={"volume_id": _uuid()}
    )
    assert resp.status_code == 404


async def test_library_404s(client):
    resp = await client.get(f"/api/v1/libraries/{_uuid()}")
    assert resp.status_code == 404
    resp = await client.put(
        f"/api/v1/libraries/{_uuid()}",
        json={"subtitle_lang_map": {"zh-CN": "zh-Hans"}},
    )
    assert resp.status_code == 404
    resp = await client.delete(f"/api/v1/libraries/{_uuid()}")
    assert resp.status_code == 404


async def test_library_delete_blocked_by_rule(client, library, rule):
    resp = await client.delete(f"/api/v1/libraries/{library.id}")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "DELETE_BLOCKED"


async def test_library_delete_blocked_by_plan(
    client, db_session, library, movie_seed
):
    await _make_plan(db_session, movie_seed["notification"], library=library)
    await db_session.commit()
    resp = await client.delete(f"/api/v1/libraries/{library.id}")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "DELETE_BLOCKED"


async def test_library_delete_allowed_with_done_plan(
    client, db_session, library, movie_seed
):
    """已执行（done）的计划不再阻止删除库：历史计划行保留、library 引用
    被解除（SET NULL 语义），只有 pending/running 计划仍然阻断。"""
    plan = await _make_plan(
        db_session, movie_seed["notification"], library=library,
        rule=None, status="done",
    )
    await db_session.commit()
    resp = await client.delete(f"/api/v1/libraries/{library.id}")
    assert resp.status_code == 200
    assert resp.json()["data"] == {"deleted": True}
    # 历史计划行保留，但引用被解除。
    row = await db_session.get(OrganizePlan, plan.id)
    await db_session.refresh(row)
    assert row is not None
    assert row.library_id is None


async def test_library_list_pending_plan_count(
    client, db_session, library, movie_seed
):
    await _make_plan(db_session, movie_seed["notification"], library=library)
    await db_session.commit()
    resp = await client.get("/api/v1/libraries")
    rows = resp.json()["data"]
    lib_row = next(r for r in rows if r["id"] == library.id)
    assert lib_row["pending_plan_count"] == 1


# ---------------------------------------------------------------------------
# Organize rules
# ---------------------------------------------------------------------------


async def test_rule_crud_roundtrip(client, library):
    resp = await client.post(
        "/api/v1/organize-rules",
        json={"name": "r1", "priority": 10, "library_id": library.id,
              "path_template": MOVIE_TEMPLATE,
              "filter": {"field": "movie.genre", "operator": "contains",
                         "value": "Horror"}},
    )
    assert resp.status_code == 201
    created = resp.json()["data"]
    assert created["priority"] == 10
    assert created["enabled"] is True
    assert created["file_op"] == "move"
    assert created["filter"]["field"] == "movie.genre"

    # List is ordered by priority asc.
    resp = await client.post(
        "/api/v1/organize-rules",
        json={"name": "r0", "priority": 5, "library_id": library.id,
              "path_template": MOVIE_TEMPLATE},
    )
    assert resp.status_code == 201
    resp = await client.get("/api/v1/organize-rules")
    names = [r["name"] for r in resp.json()["data"]]
    assert names == ["r0", "r1"]

    resp = await client.get(f"/api/v1/organize-rules/{created['id']}")
    assert resp.status_code == 200

    resp = await client.put(
        f"/api/v1/organize-rules/{created['id']}",
        json={"priority": 1, "enabled": False},
    )
    assert resp.status_code == 200
    updated = resp.json()["data"]
    assert updated["priority"] == 1
    assert updated["enabled"] is False

    resp = await client.delete(f"/api/v1/organize-rules/{created['id']}")
    assert resp.status_code == 200
    assert resp.json()["data"] == {"deleted": True}


async def test_rule_create_rejects_invalid_dsl(client, library):
    # 取值操作符缺 value → 422
    resp = await client.post(
        "/api/v1/organize-rules",
        json={"name": "bad", "library_id": library.id,
              "path_template": MOVIE_TEMPLATE,
              "filter": {"field": "movie.genre", "operator": "contains"}},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
    # 非法字段 → 422
    resp = await client.post(
        "/api/v1/organize-rules",
        json={"name": "bad", "library_id": library.id,
              "path_template": MOVIE_TEMPLATE,
              "filter": {"field": "nope.field", "operator": "eq", "value": "x"}},
    )
    assert resp.status_code == 422


async def test_rule_create_rejects_invalid_template(client, library):
    resp = await client.post(
        "/api/v1/organize-rules",
        json={"name": "bad", "library_id": library.id,
              "path_template": "{unknown_placeholder}/{title}{ext}"},
    )
    assert resp.status_code == 422
    # 绝对路径模板 → 422
    resp = await client.post(
        "/api/v1/organize-rules",
        json={"name": "bad", "library_id": library.id,
              "path_template": "/abs/{title}{ext}"},
    )
    assert resp.status_code == 422


async def test_rule_create_accepts_file_op_three_values(client, library):
    """R3：file_op 接受 move/hardlink/copy 三值。"""
    for op in ("move", "hardlink", "copy"):
        resp = await client.post(
            "/api/v1/organize-rules",
            json={"name": f"r-{op}", "library_id": library.id,
                  "path_template": MOVIE_TEMPLATE, "file_op": op},
        )
        assert resp.status_code == 201
        assert resp.json()["data"]["file_op"] == op


async def test_rule_create_rejects_invalid_file_op(client, library):
    resp = await client.post(
        "/api/v1/organize-rules",
        json={"name": "bad", "library_id": library.id,
              "path_template": MOVIE_TEMPLATE, "file_op": "symlink"},
    )
    assert resp.status_code == 422


async def test_rule_create_unknown_library_404(client):
    resp = await client.post(
        "/api/v1/organize-rules",
        json={"name": "bad", "library_id": _uuid(),
              "path_template": MOVIE_TEMPLATE},
    )
    assert resp.status_code == 404


async def test_rule_update_validation_and_404(client, library, rule):
    resp = await client.put(
        f"/api/v1/organize-rules/{rule.id}",
        json={"path_template": "{bogus}{ext}"},
    )
    assert resp.status_code == 422
    resp = await client.put(
        f"/api/v1/organize-rules/{rule.id}",
        json={"filter": {"field": "season", "operator": "eq"}},
    )
    assert resp.status_code == 422
    resp = await client.put(
        f"/api/v1/organize-rules/{rule.id}", json={"library_id": _uuid()}
    )
    assert resp.status_code == 404
    resp = await client.put(
        f"/api/v1/organize-rules/{_uuid()}", json={"name": "x"}
    )
    assert resp.status_code == 404
    resp = await client.delete(f"/api/v1/organize-rules/{_uuid()}")
    assert resp.status_code == 404


async def test_rule_delete_nulls_plan_rule_id(
    client, db_session, library, rule, movie_seed
):
    plan = await _make_plan(
        db_session, movie_seed["notification"], library=library, rule=rule
    )
    await db_session.commit()
    resp = await client.delete(f"/api/v1/organize-rules/{rule.id}")
    assert resp.status_code == 200
    await db_session.refresh(plan)
    assert plan.rule_id is None  # SET NULL 保留历史


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------


async def _point_download_dir(db_session_factory, notification_id: str, download_dir: str):
    """把通知快照与任务的 download_dir 指向 tmp 目录。

    必须用**新鲜会话**写入：长生命周期 fixture 会话持有 MVCC 快照，对已读
    行的 UPDATE 会被静默丢弃（tests/api/test_notifications_api.py 注释的
    同款坑）。
    """
    async with db_session_factory() as s:
        n = await s.get(DownloadNotification, notification_id)
        payload = dict(n.payload)
        payload["task"] = {**payload["task"], "download_dir": download_dir}
        n.payload = payload
        task = await s.get(DownloadTask, payload["task"]["download_task_id"])
        if task is not None:
            task.download_dir = download_dir
        await s.commit()


async def test_preview_with_current_rules(
    client, db_session, db_session_factory, movie_seed, download_file
):
    """first-match 当前规则列表；渲染逐文件 src→dst；不落库。"""
    lib = Library(
        id=_uuid(), name="Lib", kind="movie",
        volume_id=(await _mkvolume(db_session, download_file / "lib")),
    )
    rule = OrganizeRule(
        id=_uuid(), name="movies", priority=1, enabled=True, filter=None,
        library_id=lib.id, path_template=MOVIE_TEMPLATE,
    )
    db_session.add_all([lib, rule])
    await db_session.commit()
    n = movie_seed["notification"]
    await _point_download_dir(db_session_factory, n.id, str(download_file))

    resp = await client.post(
        "/api/v1/organize-rules/preview", json={"notification_id": n.id,
                                                "category": "Horror"}
    )
    assert resp.status_code == 200, resp.json()
    data = resp.json()["data"]
    assert data["matched_rule"]["name"] == "movies"
    assert data["library"]["id"] == lib.id
    assert data["uncategorized"] is False
    assert len(data["ops"]) == 1
    op = data["ops"][0]
    assert op["op_type"] == "move"
    assert op["src"].endswith("Test.Movie.2020.1080p.mkv")
    assert op["dst"] == str(
        download_file / "lib" / "Horror" / "Test Movie (2020)"
        / "Test Movie (2020).mkv"
    )

    # dry-run：不落任何计划
    resp = await client.get("/api/v1/organize/plans")
    assert resp.json()["meta"]["total"] == 0


async def test_preview_with_rule_draft(
    client, db_session, db_session_factory, movie_seed, download_file
):
    lib = Library(
        id=_uuid(), name="DraftLib", kind="movie",
        volume_id=(await _mkvolume(db_session, download_file / "draft")),
    )
    db_session.add(lib)
    await db_session.commit()
    n = movie_seed["notification"]
    await _point_download_dir(db_session_factory, n.id, str(download_file))

    resp = await client.post(
        "/api/v1/organize-rules/preview",
        json={
            "notification_id": n.id,
            "category": "Drama",
            "rule": {"name": "draft", "library_id": lib.id,
                     "path_template": MOVIE_TEMPLATE,
                     "filter": {"field": "movie.genre", "operator": "contains",
                                "value": "Drama"}},
        },
    )
    assert resp.status_code == 200, resp.json()
    data = resp.json()["data"]
    # filter 不匹配（快照无 genre）→ 待分类信号，无 ops
    assert data["matched_rule"] is None
    assert data["uncategorized"] is True
    assert data["ops"] == []

    resp = await client.post(
        "/api/v1/organize-rules/preview",
        json={
            "notification_id": n.id,
            "category": "Drama",
            "rule": {"name": "draft", "library_id": lib.id,
                     "path_template": MOVIE_TEMPLATE},
        },
    )
    assert resp.status_code == 200, resp.json()
    data = resp.json()["data"]
    assert data["matched_rule"]["name"] == "draft"
    assert data["ops"][0]["dst"].startswith(str(download_file / "draft"))


async def test_preview_by_resource_id(
    client, db_session, db_session_factory, library, movie_seed, download_file,
    monkeypatch,
):
    """resource_id 预览：best-effort 走下载器 RPC 取文件清单，不落库。"""
    await _point_download_dir(
        db_session_factory, movie_seed["notification"].id, str(download_file)
    )
    async with db_session_factory() as s:
        task = await s.get(DownloadTask, movie_seed["task"].id)
        task.transmission_torrent_id = 7
        # 预览按通知生成链路现构快照：真实链路在通知创建时已落 auto 关联行
        # （apply_auto_assignments + bind_single_work_assignments），快照
        # file_associations.status=complete 才可规划；这里补种等价关联行。
        s.add(ResourceFileAssignment(
            resource_id=movie_seed["resource"].id,
            file_path="Test.Movie.2020.1080p.mkv",
            file_size=100,
            movie_id=movie_seed["movie"].id,
            source="auto",
        ))
        await s.commit()
    get_files = AsyncMock(return_value={
        "name": "Test.Movie.2020.1080p.mkv",
        # 真实清单形状（{"name","size"}），主视频 ≥50MB 才进权威关联校验。
        "files": [{"name": "Test.Movie.2020.1080p.mkv", "size": 300 * 1024 * 1024}],
    })
    monkeypatch.setattr(
        "app.clients.transmission.TransmissionWrapper.get_torrent_files",
        get_files,
    )
    resp = await client.post(
        "/api/v1/organize-rules/preview",
        json={
            "resource_id": movie_seed["resource"].id,
            "category": "Horror",
            "rule": {"name": "draft", "library_id": library.id,
                     "path_template": MOVIE_TEMPLATE},
        },
    )
    assert resp.status_code == 200, resp.json()
    data = resp.json()["data"]
    assert data["ops"][0]["dst"].endswith("Test Movie (2020).mkv")
    resp = await client.get("/api/v1/organize/plans")
    assert resp.json()["meta"]["total"] == 0  # 不落库


async def test_preview_validation_errors(client, movie_seed):
    # 两个目标都缺 → 422（pydantic model_validator）
    resp = await client.post("/api/v1/organize-rules/preview", json={})
    assert resp.status_code == 422
    # 不存在的 notification → 404
    resp = await client.post(
        "/api/v1/organize-rules/preview", json={"notification_id": _uuid()}
    )
    assert resp.status_code == 404
    # 不存在的 resource → 404
    resp = await client.post(
        "/api/v1/organize-rules/preview", json={"resource_id": _uuid()}
    )
    assert resp.status_code == 404
    # 草稿模板非法 → 422
    resp = await client.post(
        "/api/v1/organize-rules/preview",
        json={"notification_id": movie_seed["notification"].id,
              "rule": {"name": "x", "library_id": _uuid(),
                       "path_template": "{bogus}{ext}"}},
    )
    assert resp.status_code == 422
    # 草稿指向不存在的库 → 404
    resp = await client.post(
        "/api/v1/organize-rules/preview",
        json={"notification_id": movie_seed["notification"].id,
              "rule": {"name": "x", "library_id": _uuid(),
                       "path_template": MOVIE_TEMPLATE}},
    )
    assert resp.status_code == 404


async def test_preview_uncategorized_when_no_rule_matches(
    client, db_session_factory, movie_seed, download_file
):
    n = movie_seed["notification"]
    await _point_download_dir(db_session_factory, n.id, str(download_file))
    resp = await client.post(
        "/api/v1/organize-rules/preview", json={"notification_id": n.id}
    )
    assert resp.status_code == 200, resp.json()
    data = resp.json()["data"]
    assert data["uncategorized"] is True
    assert data["matched_rule"] is None


# ---------------------------------------------------------------------------
# Plans: list / detail
# ---------------------------------------------------------------------------


async def _seed_plan_with_op(db_session, notification, library, **kw):
    plan = await _make_plan(db_session, notification, library=library, **kw)
    db_session.add(
        OrganizePlanOp(
            id=_uuid(), plan_id=plan.id, seq=0, op_type="move",
            src="/downloads/x/a.mkv", dst="/media/movies/a.mkv", size=100,
        )
    )
    db_session.add(
        OrganizePlanOp(
            id=_uuid(), plan_id=plan.id, seq=1, op_type="keep",
            src="/downloads/x/note.txt", dst=None, size=10,
        )
    )
    db_session.add(
        OrganizeAuditEntry(
            id=_uuid(), plan_id=plan.id, action="plan_created",
            detail={"ops": 2},
        )
    )
    await db_session.commit()
    return plan


async def test_plan_list_filters_and_pagination(
    client, db_session, library, movie_seed, sample_channel, sample_downloader
):
    plan = await _seed_plan_with_op(
        db_session, movie_seed["notification"], library
    )
    # 第二条通知 + 计划（failed，无库=待分类）
    task2 = DownloadTask(
        id=_uuid(), agent_id=movie_seed["agent"].id,
        file_resource_id=movie_seed["resource"].id,
        downloader_id=sample_downloader.id, download_dir="/downloads/x",
        transmission_torrent_id=None, status="completed", completed_at=utcnow(),
    )
    db_session.add(task2)
    await db_session.flush()
    n2 = DownloadNotification(
        id=_uuid(), agent_id=movie_seed["agent"].id,
        download_task_id=task2.id, payload=movie_seed["payload"],
    )
    db_session.add(n2)
    await db_session.flush()
    plan2 = await _make_plan(db_session, n2, status="failed")
    await db_session.commit()

    resp = await client.get("/api/v1/organize/plans")
    body = resp.json()
    assert body["meta"]["total"] == 2
    item = body["data"][0]
    assert "payload" not in item  # 列表项不含快照
    assert "ops_summary" in item
    assert item["library_name"] is None or item["library_name"] == "Movies"

    resp = await client.get("/api/v1/organize/plans?status=pending")
    assert resp.json()["meta"]["total"] == 1
    resp = await client.get("/api/v1/organize/plans?status=failed")
    rows = resp.json()["data"]
    assert len(rows) == 1 and rows[0]["id"] == plan2.id
    resp = await client.get(f"/api/v1/organize/plans?library_id={library.id}")
    rows = resp.json()["data"]
    assert len(rows) == 1 and rows[0]["id"] == plan.id
    assert rows[0]["library_name"] == "Movies"
    assert rows[0]["ops_summary"] == {"total": 2, "move": 1, "keep": 1,
                                      "movedir": 0}
    assert [op["seq"] for op in rows[0]["ops_preview"]] == [0, 1]
    assert rows[0]["ops_preview"][0]["src"] == "/downloads/x/a.mkv"

    # 分页
    resp = await client.get("/api/v1/organize/plans?page=2&page_size=1")
    body = resp.json()
    assert body["meta"]["total"] == 2
    assert len(body["data"]) == 1

    # 未知状态 → 422
    resp = await client.get("/api/v1/organize/plans?status=bogus")
    assert resp.status_code == 422


async def test_plan_pending_reason_unclassified(client, db_session, movie_seed):
    """library 未定的 pending 计划 → pending_reason=unclassified。"""
    await _make_plan(db_session, movie_seed["notification"])
    await db_session.commit()
    resp = await client.get("/api/v1/organize/plans")
    [item] = resp.json()["data"]
    assert item["status"] == "pending"
    assert item["pending_reason"] == "unclassified"


async def test_plan_list_ops_preview_capped(client, db_session, library, movie_seed):
    """列表项仅带前 3 条 op 预览（按 seq 排序），完整 op 走详情。"""
    plan = await _make_plan(db_session, movie_seed["notification"], library=library)
    for seq in range(5):
        db_session.add(
            OrganizePlanOp(
                id=_uuid(), plan_id=plan.id, seq=seq, op_type="move",
                src=f"/downloads/x/f{seq}.mkv", dst=f"/media/f{seq}.mkv", size=100,
            )
        )
    await db_session.commit()
    resp = await client.get("/api/v1/organize/plans")
    [item] = resp.json()["data"]
    assert item["ops_summary"]["total"] == 5
    assert len(item["ops_preview"]) == 3
    assert [op["seq"] for op in item["ops_preview"]] == [0, 1, 2]
    assert item["ops_preview"][0]["src"] == "/downloads/x/f0.mkv"


async def test_plan_pending_reason_unbound_and_execute_409(
    client, db_session, movie_seed
):
    """目标库未绑定卷 → pending_reason=unbound；执行门禁 409。"""
    unbound_lib = await _make_library(
        db_session, "/media/unused", name="Unbound", bound=False
    )
    plan = await _seed_plan_with_op(
        db_session, movie_seed["notification"], unbound_lib
    )
    resp = await client.get("/api/v1/organize/plans")
    [item] = resp.json()["data"]
    assert item["pending_reason"] == "unbound"
    # 详情同样带派生字段
    resp = await client.get(f"/api/v1/organize/plans/{plan.id}")
    assert resp.json()["data"]["pending_reason"] == "unbound"
    # 执行门禁拒绝待绑定
    resp = await client.post(f"/api/v1/organize/plans/{plan.id}/execute")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "INVALID_STATE"


async def test_plan_detail_includes_ops_payload_audit(
    client, db_session, library, rule, movie_seed
):
    plan = await _seed_plan_with_op(
        db_session, movie_seed["notification"], library, rule=rule,
        category="Horror",
    )
    resp = await client.get(f"/api/v1/organize/plans/{plan.id}")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["payload"]["notification_id"] == "n1"
    assert data["rule_name"] == "all-movies"
    assert data["library_name"] == "Movies"
    assert data["category"] == "Horror"
    assert len(data["ops"]) == 2
    assert {o["op_type"] for o in data["ops"]} == {"move", "keep"}
    assert len(data["audit_entries"]) == 1
    assert data["audit_entries"][0]["action"] == "plan_created"

    resp = await client.get(f"/api/v1/organize/plans/{_uuid()}")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Plans: execute / execute-batch / classify / cancel
# ---------------------------------------------------------------------------


async def test_execute_rejects_non_pending_failed(
    client, db_session, library, movie_seed
):
    for status in ("done", "cancelled"):
        plan = await _seed_plan_with_op(
            db_session, movie_seed["notification"], library, status=status
        )
        resp = await client.post(f"/api/v1/organize/plans/{plan.id}/execute")
        assert resp.status_code == 409, status
        assert resp.json()["error"]["code"] == "INVALID_STATE"
        await db_session.delete(plan)
        await db_session.commit()


async def test_execute_allows_stale_running(
    client, db_session, library, movie_seed, monkeypatch
):
    """崩溃遗留的 running（本进程未在执行）可重放；正在执行的拒绝。"""
    plan = await _seed_plan_with_op(
        db_session, movie_seed["notification"], library, status="running"
    )
    schedule = MagicMock()
    monkeypatch.setattr(
        "app.services.organize_service.schedule_auto_execute", schedule
    )
    resp = await client.post(f"/api/v1/organize/plans/{plan.id}/execute")
    assert resp.status_code == 202
    schedule.assert_called_once_with(plan.id)

    monkeypatch.setattr(
        "app.services.organize_service.is_plan_executing", lambda _id: True
    )
    resp = await client.post(f"/api/v1/organize/plans/{plan.id}/execute")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "ALREADY_RUNNING"


async def test_execute_rejects_uncategorized(
    client, db_session, movie_seed
):
    plan = await _make_plan(db_session, movie_seed["notification"])
    await db_session.commit()
    resp = await client.post(f"/api/v1/organize/plans/{plan.id}/execute")
    assert resp.status_code == 409

    resp = await client.post(f"/api/v1/organize/plans/{_uuid()}/execute")
    assert resp.status_code == 404


async def test_execute_rejects_missing_category(
    client, db_session, library, rule, movie_seed
):
    plan = await _seed_plan_with_op(
        db_session, movie_seed["notification"], library, rule=rule
    )
    resp = await client.post(f"/api/v1/organize/plans/{plan.id}/execute")
    assert resp.status_code == 409


async def test_execute_accepts_pending_and_schedules(
    client, db_session, library, movie_seed, monkeypatch
):
    plan = await _seed_plan_with_op(
        db_session, movie_seed["notification"], library
    )
    schedule = MagicMock()
    monkeypatch.setattr(
        "app.services.organize_service.schedule_auto_execute", schedule
    )
    resp = await client.post(f"/api/v1/organize/plans/{plan.id}/execute")
    assert resp.status_code == 202
    assert resp.json()["data"]["status"] == "pending"
    schedule.assert_called_once_with(plan.id)


async def test_execute_batch(
    client, db_session, library, movie_seed, monkeypatch
):
    plan = await _make_plan(db_session, movie_seed["notification"],
                            library=library)
    await db_session.commit()
    fake = AsyncMock(return_value=[(plan.id, "done"), ("missing", "计划不存在")])
    monkeypatch.setattr("app.services.organize_service.execute_plans", fake)
    resp = await client.post(
        "/api/v1/organize/plans/execute-batch",
        json={"plan_ids": [plan.id, "missing"]},
    )
    assert resp.status_code == 200
    results = resp.json()["data"]["results"]
    assert results == [
        {"plan_id": plan.id, "status": "done"},
        {"plan_id": "missing", "status": "计划不存在"},
    ]


async def test_classify_uncategorized_plan(
    client, db_session, library, movie_seed
):
    """待分类计划指定 library + category → 重渲染 op dst。"""
    plan = await _seed_plan_with_op(db_session, movie_seed["notification"], None)
    assert plan.library_id is None

    resp = await client.post(
        f"/api/v1/organize/plans/{plan.id}/classify",
        json={"library_id": library.id, "category": "Horror"},
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["library_id"] == library.id
    assert data["category"] == "Horror"
    assert data["status"] == "pending"

    resp = await client.get(f"/api/v1/organize/plans/{plan.id}")
    data = resp.json()["data"]
    move_op = next(o for o in data["ops"] if o["op_type"] == "move")
    assert move_op["dst"] == (
        "/media/movies/Horror/Test Movie (2020)/Test Movie (2020).mkv"
    )
    # 审计记录 classify 动作
    actions = [a["action"] for a in data["audit_entries"]]
    assert "classify" in actions


async def test_classify_rejects_bad_state_and_library(
    client, db_session, library, movie_seed
):
    plan = await _make_plan(db_session, movie_seed["notification"],
                            status="done", library=library)
    await db_session.commit()
    resp = await client.post(
        f"/api/v1/organize/plans/{plan.id}/classify",
        json={"library_id": library.id},
    )
    assert resp.status_code == 409
    await db_session.delete(plan)
    await db_session.commit()

    # notification_id 唯一：删掉上面的计划后复用同一通知
    pending = await _make_plan(db_session, movie_seed["notification"])
    await db_session.commit()
    resp = await client.post(
        f"/api/v1/organize/plans/{pending.id}/classify",
        json={"library_id": _uuid()},
    )
    assert resp.status_code == 404

    resp = await client.post(
        f"/api/v1/organize/plans/{_uuid()}/classify",
        json={"library_id": library.id},
    )
    assert resp.status_code == 404


async def test_cancel_plan(client, db_session, library, movie_seed):
    plan = await _make_plan(db_session, movie_seed["notification"],
                            library=library)
    await db_session.commit()
    resp = await client.post(f"/api/v1/organize/plans/{plan.id}/cancel")
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "cancelled"
    await db_session.refresh(plan)
    assert plan.status == "cancelled"

    # 已取消不可再取消
    resp = await client.post(f"/api/v1/organize/plans/{plan.id}/cancel")
    assert resp.status_code == 409
    resp = await client.post(f"/api/v1/organize/plans/{_uuid()}/cancel")
    assert resp.status_code == 404


async def test_cancel_stale_running_plan(
    client, db_session, library, movie_seed, monkeypatch
):
    """崩溃遗留的 running 可取消；本进程正在执行的 running 拒绝。"""
    plan = await _make_plan(db_session, movie_seed["notification"],
                            library=library, status="running")
    await db_session.commit()
    resp = await client.post(f"/api/v1/organize/plans/{plan.id}/cancel")
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "cancelled"

    monkeypatch.setattr(
        "app.services.organize_service.is_plan_executing", lambda _id: True
    )
    # notification_id 唯一：删掉上面的计划后复用同一通知
    await db_session.delete(plan)
    await db_session.commit()
    plan2 = await _seed_plan_with_op(
        db_session, movie_seed["notification"], library, status="running"
    )
    resp = await client.post(f"/api/v1/organize/plans/{plan2.id}/cancel")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "ALREADY_RUNNING"


async def test_cancel_plan_delete_task_and_data(
    client, db_session, library, movie_seed, sample_downloader, mock_transmission
):
    """取消计划可附带删除下载任务；delete_data 蕴含删除任务并删磁盘数据。

    任务清理与 ``DELETE /tasks/{id}`` 共用 task_cleanup 实现：移除下载器
    torrent、任务行置 cancelled；清理结果随响应 task_cleaned 返回。
    """
    seed = movie_seed

    async def new_plan(torrent_id):
        task = DownloadTask(
            id=_uuid(), agent_id=seed["agent"].id,
            file_resource_id=seed["resource"].id,
            downloader_id=sample_downloader.id, download_dir="/downloads/x",
            transmission_torrent_id=torrent_id, status="completed",
            completed_at=utcnow(),
        )
        db_session.add(task)
        await db_session.flush()
        notification = DownloadNotification(
            id=_uuid(), agent_id=seed["agent"].id, download_task_id=task.id,
            payload=seed["payload"],
        )
        db_session.add(notification)
        await db_session.flush()
        plan = await _make_plan(db_session, notification, library=library)
        await db_session.commit()
        return plan, task

    # delete_task=True：移除 torrent、保留磁盘数据，任务置 cancelled。
    plan, task = await new_plan(42)
    resp = await client.post(
        f"/api/v1/organize/plans/{plan.id}/cancel", json={"delete_task": True}
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "cancelled"
    assert resp.json()["data"]["task_cleaned"] is True
    mock_transmission.remove_torrent.assert_awaited_once_with(42, delete_data=False)
    await db_session.refresh(task)
    assert task.status == "cancelled"

    # delete_data=True 蕴含 delete_task：连同磁盘数据一起删除。
    mock_transmission.remove_torrent.reset_mock()
    plan2, task2 = await new_plan(43)
    resp = await client.post(
        f"/api/v1/organize/plans/{plan2.id}/cancel", json={"delete_data": True}
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["task_cleaned"] is True
    mock_transmission.remove_torrent.assert_awaited_once_with(43, delete_data=True)
    await db_session.refresh(task2)
    assert task2.status == "cancelled"

    # 默认（无 body）：不动下载任务，task_cleaned 为 null。
    mock_transmission.remove_torrent.reset_mock()
    plan3, task3 = await new_plan(44)
    resp = await client.post(f"/api/v1/organize/plans/{plan3.id}/cancel")
    assert resp.status_code == 200
    assert resp.json()["data"]["task_cleaned"] is None
    mock_transmission.remove_torrent.assert_not_awaited()
    await db_session.refresh(task3)
    assert task3.status == "completed"


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


async def test_audit_list_and_plan_filter(
    client, db_session, library, movie_seed, sample_downloader
):
    plan = await _make_plan(db_session, movie_seed["notification"],
                            library=library)
    await db_session.flush()
    for action in ("plan_created", "execute", "cancelled"):
        db_session.add(
            OrganizeAuditEntry(id=_uuid(), plan_id=plan.id, action=action,
                               detail=None)
        )
    # 另一计划的条目（过滤应排除；notification_id 唯一，需另建通知）
    task2 = DownloadTask(
        id=_uuid(), agent_id=movie_seed["agent"].id,
        file_resource_id=movie_seed["resource"].id,
        downloader_id=sample_downloader.id, download_dir="/downloads/x",
        transmission_torrent_id=None, status="completed", completed_at=utcnow(),
    )
    db_session.add(task2)
    await db_session.flush()
    n2 = DownloadNotification(
        id=_uuid(), agent_id=movie_seed["agent"].id,
        download_task_id=task2.id, payload=movie_seed["payload"],
    )
    db_session.add(n2)
    await db_session.flush()
    other = OrganizePlan(
        id=_uuid(), notification_id=n2.id,
        status="pending", payload=movie_seed["payload"],
    )
    db_session.add(other)
    await db_session.flush()
    db_session.add(
        OrganizeAuditEntry(id=_uuid(), plan_id=other.id, action="plan_created",
                           detail=None)
    )
    await db_session.commit()

    resp = await client.get("/api/v1/organize/audit")
    body = resp.json()
    assert body["meta"]["total"] == 4  # plan 3 条 + other 1 条
    assert body["meta"]["page"] == 1

    resp = await client.get(f"/api/v1/organize/audit?plan_id={plan.id}")
    body = resp.json()
    assert body["meta"]["total"] == 3
    assert all(e["plan_id"] == plan.id for e in body["data"])

    resp = await client.get("/api/v1/organize/audit?page=2&page_size=3")
    body = resp.json()
    assert body["meta"]["total"] == 4
    assert len(body["data"]) == 1
