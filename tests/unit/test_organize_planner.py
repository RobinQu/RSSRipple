"""整理规划器（organize_planner）单元测试。

payload fixture 形态借鉴 vault-organizer tests/fixtures/notifications/*.json。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.organize_planner import (
    DiskFile,
    PlanError,
    build_filter_context,
    build_plan,
    translate_path,
)
from app.services.organize_template import PRESET_MOVIE, PRESET_TV


def _library(lib_id="lib-tv", root="/data/tv", lang_map=None, kind="tv"):
    return SimpleNamespace(
        id=lib_id,
        name=lib_id,
        root_path=root,
        kind=kind,
        plex_section=None,
        subtitle_lang_map=lang_map,
    )


def _rule(name, priority, library_id, template, filter=None, enabled=True):
    return SimpleNamespace(
        id=f"rule-{name}",
        name=name,
        priority=priority,
        enabled=enabled,
        filter=filter,
        library_id=library_id,
        path_template=template,
        file_op="move",
        auto_execute=False,
    )


# 单集 payload（形态参考 vault-organizer fixtures 的 gits_e04.json）
GITS_PAYLOAD = {
    "notification_id": "n-1",
    "agent": {"id": "a-1", "name": "mikan"},
    "task": {
        "download_task_id": "t-1",
        "download_dir": "/downloads/complete",
        "torrent_name": None,
    },
    "resource": {
        "title_raw": "[❀拨雪寻春❀] 攻壳机动队 - 04 [WebRip 1080p]",
        "season": 1,
        "episode": 4,
        "is_batch": False,
        "episode_start": None,
        "episode_end": None,
        "subtitle_langs": ["zh-CN", "zh-TW"],
        "resolution": "1080p",
        "container": None,
        "title_year": None,
    },
    "work": {
        "type": "series",
        "series_id": "s-1",
        "title_en": "THE GHOST IN THE SHELL",
        "title_cn": "攻壳机动队",
        "original_title": "攻殻機動隊 THE GHOST IN THE SHELL",
        "year": 2026,
        "content_type": "tv",
        "is_anime": True,
        "collection": "攻壳机动队（系列）",
        "genre": ["Animation", "Science Fiction"],
        "seasons": [{"season_number": 1, "episode_count": 10}],
        "episodes": [
            {"season": 1, "episode": 4, "title": "机器人回旋曲"},
        ],
    },
}

# 电影 payload（形态参考 hamnet.json）
HAMNET_PAYLOAD = {
    "notification_id": "n-2",
    "agent": {"id": "a-2", "name": "movies"},
    "task": {
        "download_task_id": "t-2",
        "download_dir": "/downloads/complete",
        "torrent_name": None,
    },
    "resource": {
        "title_raw": "Hamnet.2025.COMPLETE.2160p.UHD.BLURAY-TDi",
        "season": None,
        "episode": None,
        "is_batch": False,
        "episode_start": None,
        "episode_end": None,
        "subtitle_langs": [],
        "resolution": "2160p",
        "container": None,
        "title_year": None,
    },
    "work": {
        "type": "movie",
        "movie_id": "m-1",
        "title_en": "Hamnet",
        "title_cn": "哈姆奈特",
        "original_title": "Hamnet (2025)",
        "year": 2025,
        "content_type": "movie",
        "is_anime": False,
        "collection": None,
        "genre": ["Horror", "Drama"],
        "seasons": None,
        "episodes": None,
    },
}


def _batch_payload():
    payload = {
        **GITS_PAYLOAD,
        "notification_id": "n-3",
        "resource": {
            **GITS_PAYLOAD["resource"],
            "season": 1,
            "episode": None,
            "is_batch": True,
            "episode_start": 1,
            "episode_end": 3,
        },
        "work": {
            **GITS_PAYLOAD["work"],
            "episodes": [
                {"season": 1, "episode": 1, "title": "第一集"},
                {"season": 1, "episode": 2, "title": "第二集"},
                {"season": 1, "episode": 3, "title": "第三集"},
            ],
        },
    }
    return payload


def _batch_files(base="/downloads/complete/gits"):
    return [
        DiskFile(path=f"{base}/GITS.S01E01.1080p.mkv", size=300, rel="GITS.S01E01.1080p.mkv"),
        DiskFile(path=f"{base}/GITS.S01E02.1080p.mkv", size=300, rel="GITS.S01E02.1080p.mkv"),
        DiskFile(path=f"{base}/GITS.S01E03.1080p.mkv", size=300, rel="GITS.S01E03.1080p.mkv"),
    ]


class TestSingleEpisode:
    def test_tv_template_paths(self):
        lib = _library("lib-anime", "/data/tv_anime")
        rule = _rule("anime", 10, lib.id, PRESET_TV)
        files = [
            DiskFile("/downloads/complete/gits/ep04.mkv", 1000, "ep04.mkv"),
            DiskFile("/downloads/complete/gits/ep04.chs.ass", 5, "ep04.chs.ass"),
            DiskFile("/downloads/complete/gits/info.nfo", 1, "info.nfo"),
        ]
        result = build_plan(GITS_PAYLOAD, files, [rule], [lib])
        assert result.rule is rule
        assert result.library is lib
        assert not result.needs_category
        by_src = {op.src: op for op in result.ops}
        main = by_src["/downloads/complete/gits/ep04.mkv"]
        assert main.op_type == "move"
        assert main.dst == (
            "/data/tv_anime/攻壳机动队/Season 01/"
            "攻壳机动队 - s01e04 - 机器人回旋曲.mkv"
        )
        sub = by_src["/downloads/complete/gits/ep04.chs.ass"]
        assert sub.op_type == "move"
        assert sub.dst == (
            "/data/tv_anime/攻壳机动队/Season 01/"
            "攻壳机动队 - s01e04 - 机器人回旋曲.chs.ass"
        )
        nfo = by_src["/downloads/complete/gits/info.nfo"]
        assert nfo.op_type == "keep" and nfo.dst is None

    def test_largest_video_is_main(self):
        lib = _library()
        rule = _rule("tv", 10, lib.id, PRESET_TV)
        files = [
            DiskFile("/d/sample.mkv", 10, "sample.mkv"),
            DiskFile("/d/main.mkv", 1000, "main.mkv"),
        ]
        result = build_plan(GITS_PAYLOAD, files, [rule], [lib])
        by_src = {op.src: op for op in result.ops}
        assert by_src["/d/main.mkv"].op_type == "move"
        assert by_src["/d/sample.mkv"].op_type == "keep"

    def test_missing_episode_fails(self):
        payload = {
            **GITS_PAYLOAD,
            "resource": {**GITS_PAYLOAD["resource"], "episode": None},
        }
        lib = _library()
        rule = _rule("tv", 10, lib.id, PRESET_TV)
        with pytest.raises(PlanError, match="集号"):
            build_plan(payload, [DiskFile("/d/a.mkv", 1, "a.mkv")], [rule], [lib])

    def test_no_videos_fails(self):
        lib = _library()
        rule = _rule("tv", 10, lib.id, PRESET_TV)
        with pytest.raises(PlanError, match="视频"):
            build_plan(GITS_PAYLOAD, [DiskFile("/d/a.nfo", 1, "a.nfo")], [rule], [lib])


class TestBatch:
    def test_per_file_dst(self):
        lib = _library("lib-anime", "/data/tv_anime")
        rule = _rule("anime", 10, lib.id, PRESET_TV)
        files = _batch_files() + [
            DiskFile(
                "/downloads/complete/gits/GITS.S01E01.chs.srt",
                5,
                "GITS.S01E01.chs.srt",
            ),
            DiskFile("/downloads/complete/gits/sp.mkvx", 1, "sp.mkvx"),
            DiskFile("/downloads/complete/gits/特典.mkv", 50, "特典.mkv"),
        ]
        result = build_plan(_batch_payload(), files, [rule], [lib])
        moves = {op.src: op.dst for op in result.ops if op.op_type == "move"}
        assert moves["/downloads/complete/gits/GITS.S01E02.1080p.mkv"] == (
            "/data/tv_anime/攻壳机动队/Season 01/"
            "攻壳机动队 - s01e02 - 第二集.mkv"
        )
        assert moves["/downloads/complete/gits/GITS.S01E01.chs.srt"] == (
            "/data/tv_anime/攻壳机动队/Season 01/"
            "攻壳机动队 - s01e01 - 第一集.chs.srt"
        )
        keeps = {op.src for op in result.ops if op.op_type == "keep"}
        # 解析不出集号的视频按特典 keep；非媒体文件 keep
        assert "/downloads/complete/gits/特典.mkv" in keeps
        assert "/downloads/complete/gits/sp.mkvx" in keeps

    def test_missing_episode_rejected(self):
        lib = _library()
        rule = _rule("tv", 10, lib.id, PRESET_TV)
        files = _batch_files()[:2]  # 缺 E03
        with pytest.raises(PlanError, match="覆盖度不足"):
            build_plan(_batch_payload(), files, [rule], [lib])

    def test_duplicate_episode_rejected(self):
        lib = _library()
        rule = _rule("tv", 10, lib.id, PRESET_TV)
        files = _batch_files() + [
            DiskFile(
                "/downloads/complete/gits/GITS.S01E01.v2.mkv",
                300,
                "GITS.S01E01.v2.mkv",
            ),
        ]
        with pytest.raises(PlanError, match="重复集号"):
            build_plan(_batch_payload(), files, [rule], [lib])

    def test_season_fallback_from_rel_path(self):
        payload = _batch_payload()
        payload["resource"] = {**payload["resource"], "season": 2}
        lib = _library()
        rule = _rule("tv", 10, lib.id, PRESET_TV)
        files = [
            DiskFile(f"/d/Season 2/GITS - 0{e}.mkv", 300, f"Season 2/GITS - 0{e}.mkv")
            for e in (1, 2, 3)
        ]
        result = build_plan(payload, files, [rule], [lib])
        for op in result.ops:
            if op.op_type == "move":
                assert "/Season 02/" in op.dst


class TestMovie:
    def test_category_dir_from_rule(self):
        lib = _library("lib-movies", "/data/movies", kind="movie")
        rule = _rule(
            "horror",
            10,
            lib.id,
            "Horror/{title} ({year})/{title} ({year}){ext}",
            filter={"field": "movie.genre", "operator": "contains", "value": "Horror"},
        )
        files = [DiskFile("/downloads/complete/hamnet/movie.mkv", 8000, "movie.mkv")]
        result = build_plan(HAMNET_PAYLOAD, files, [rule], [lib])
        assert result.rule is rule
        (op,) = result.ops
        assert op.dst == "/data/movies/Horror/哈姆奈特 (2025)/哈姆奈特 (2025).mkv"

    def test_template_with_category_placeholder_unresolved(self):
        lib = _library("lib-movies", "/data/movies", kind="movie")
        rule = _rule("movies", 10, lib.id, PRESET_MOVIE)
        files = [DiskFile("/d/movie.mkv", 8000, "movie.mkv")]
        result = build_plan(HAMNET_PAYLOAD, files, [rule], [lib])
        assert result.needs_category
        assert result.rule is rule
        assert result.ops == []

    def test_template_with_category_placeholder_resolved(self):
        lib = _library("lib-movies", "/data/movies", kind="movie")
        rule = _rule("movies", 10, lib.id, PRESET_MOVIE)
        files = [DiskFile("/d/movie.mkv", 8000, "movie.mkv")]
        result = build_plan(HAMNET_PAYLOAD, files, [rule], [lib], category="Fiction")
        assert not result.needs_category
        (op,) = result.ops
        assert op.dst == "/data/movies/Fiction/哈姆奈特 (2025)/哈姆奈特 (2025).mkv"


class TestRuleRouting:
    def test_is_anime_dsl_routing(self):
        anime_lib = _library("lib-anime", "/data/tv_anime")
        tv_lib = _library("lib-tv", "/data/tv")
        anime_rule = _rule(
            "anime",
            10,
            anime_lib.id,
            PRESET_TV,
            filter={"field": "series.is_anime", "operator": "eq", "value": True},
        )
        catch_all = _rule("tv", 20, tv_lib.id, PRESET_TV)
        files = [DiskFile("/d/ep04.mkv", 1000, "ep04.mkv")]

        anime_payload = {
            **GITS_PAYLOAD,
            "work": {**GITS_PAYLOAD["work"], "is_anime": True},
        }
        result = build_plan(anime_payload, files, [anime_rule, catch_all], [anime_lib, tv_lib])
        assert result.library is anime_lib

        # is_anime=None（未判定）：eq 不通过（空值语义），落 catch-all
        unset_payload = {
            **GITS_PAYLOAD,
            "work": {**GITS_PAYLOAD["work"], "is_anime": None},
        }
        result = build_plan(unset_payload, files, [anime_rule, catch_all], [anime_lib, tv_lib])
        assert result.library is tv_lib

    def test_first_match_wins_by_priority(self):
        lib_a = _library("lib-a", "/data/a")
        lib_b = _library("lib-b", "/data/b")
        low = _rule("low", 20, lib_b.id, PRESET_TV)
        high = _rule("high", 10, lib_a.id, PRESET_TV)
        files = [DiskFile("/d/ep04.mkv", 1000, "ep04.mkv")]
        # 传入顺序与 priority 相反，仍按 priority 命中
        result = build_plan(GITS_PAYLOAD, files, [low, high], [lib_a, lib_b])
        assert result.rule is high

    def test_disabled_rule_skipped(self):
        lib = _library()
        disabled = _rule("off", 10, lib.id, PRESET_TV, enabled=False)
        files = [DiskFile("/d/ep04.mkv", 1000, "ep04.mkv")]
        result = build_plan(GITS_PAYLOAD, files, [disabled], [lib])
        assert result.uncategorized

    def test_no_match_returns_uncategorized_signal(self):
        lib = _library()
        rule = _rule(
            "anime-only",
            10,
            lib.id,
            PRESET_TV,
            filter={"field": "series.is_anime", "operator": "eq", "value": True},
        )
        payload = {
            **GITS_PAYLOAD,
            "work": {**GITS_PAYLOAD["work"], "is_anime": False},
        }
        files = [DiskFile("/d/ep04.mkv", 1000, "ep04.mkv")]
        result = build_plan(payload, files, [rule], [lib])
        assert result.uncategorized
        assert result.rule is None
        assert result.library is None
        assert result.ops == []

    def test_missing_library_fails(self):
        rule = _rule("tv", 10, "no-such-lib", PRESET_TV)
        files = [DiskFile("/d/ep04.mkv", 1000, "ep04.mkv")]
        with pytest.raises(PlanError, match="Library 不存在"):
            build_plan(GITS_PAYLOAD, files, [rule], [])


class TestConflictPrecheck:
    def test_existing_dst_with_wrong_size_fails(self, tmp_path):
        root = tmp_path / "tv"
        existing = root / "攻壳机动队" / "Season 01" / "攻壳机动队 - s01e04 - 机器人回旋曲.mkv"
        existing.parent.mkdir(parents=True)
        existing.write_bytes(b"x" * 999)
        lib = _library("lib-tv", str(root))
        rule = _rule("tv", 10, lib.id, PRESET_TV)
        files = [DiskFile("/d/ep04.mkv", 1000, "ep04.mkv")]
        with pytest.raises(PlanError, match="拒绝覆盖"):
            build_plan(GITS_PAYLOAD, files, [rule], [lib])

    def test_existing_dst_with_same_size_is_replay(self, tmp_path):
        root = tmp_path / "tv"
        existing = root / "攻壳机动队" / "Season 01" / "攻壳机动队 - s01e04 - 机器人回旋曲.mkv"
        existing.parent.mkdir(parents=True)
        existing.write_bytes(b"x" * 1000)
        lib = _library("lib-tv", str(root))
        rule = _rule("tv", 10, lib.id, PRESET_TV)
        files = [DiskFile("/d/ep04.mkv", 1000, "ep04.mkv")]
        result = build_plan(GITS_PAYLOAD, files, [rule], [lib])
        assert result.ops[0].op_type == "move"


class TestTranslatePath:
    def test_longest_prefix_wins(self):
        m = {"/downloads": "/a", "/downloads/complete": "/b"}
        assert translate_path("/downloads/complete/x.mkv", m) == "/b/x.mkv"
        assert translate_path("/downloads/other/x.mkv", m) == "/a/other/x.mkv"

    def test_no_hit_is_identity(self):
        assert translate_path("/mnt/x.mkv", {"/downloads": "/a"}) == "/mnt/x.mkv"
        assert translate_path("/downloads2/x.mkv", {"/downloads": "/a"}) == "/downloads2/x.mkv"

    def test_none_map_is_identity(self):
        assert translate_path("/downloads/x.mkv", None) == "/downloads/x.mkv"


class TestFilterContext:
    def test_adapter_exposes_snapshot_fields(self):
        from app.services.filter_engine import evaluate_filter_config

        ctx = build_filter_context(GITS_PAYLOAD)
        assert evaluate_filter_config(
            {"field": "series.is_anime", "operator": "eq", "value": True}, ctx
        )
        assert evaluate_filter_config(
            {"field": "series.year", "operator": "eq", "value": 2026}, ctx
        )
        assert evaluate_filter_config(
            {"field": "series.collection", "operator": "contains", "value": "攻壳"},
            ctx,
        )
        assert evaluate_filter_config(
            {"field": "season", "operator": "eq", "value": 1}, ctx
        )
