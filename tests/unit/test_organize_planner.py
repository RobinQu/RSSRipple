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

    def test_file_association_missing_season_falls_back_to_resource(self):
        payload = {
            **GITS_PAYLOAD,
            "file_associations": {
                "version": 1,
                "status": "complete",
                "items": [{
                    "file_path": "ep04.mkv",
                    "work_type": "series",
                    "work_id": "s-1",
                    "season": None,
                    "episode_start": 4,
                    "episode_end": 4,
                    "source": "auto",
                }],
            },
        }
        lib = _library()
        rule = _rule("tv", 10, lib.id, PRESET_TV)
        result = build_plan(
            payload, [DiskFile("/d/ep04.mkv", 1, "ep04.mkv")], [rule], [lib]
        )
        assert result.ops[0].dst.endswith("s01e04 - 机器人回旋曲.mkv")

    def test_no_videos_fails(self):
        lib = _library()
        rule = _rule("tv", 10, lib.id, PRESET_TV)
        with pytest.raises(PlanError, match="视频"):
            build_plan(GITS_PAYLOAD, [DiskFile("/d/a.nfo", 1, "a.nfo")], [rule], [lib])


class TestBatch:
    @staticmethod
    def _multi_work_payload(second_is_anime=True):
        first = {**GITS_PAYLOAD["work"], "series_id": "s-1", "title_cn": "作品甲"}
        second = {
            **GITS_PAYLOAD["work"], "series_id": "s-2", "title_cn": "作品乙",
            "is_anime": second_is_anime,
        }
        items = [
            {
                "file_path": "A/ep.mkv", "file_size": 300,
                "work_type": "series", "work_id": "s-1", "season": 1,
                "episode_start": 1, "episode_end": 1, "source": "manual",
            },
            {
                "file_path": "B/ep.mkv", "file_size": 300,
                "work_type": "series", "work_id": "s-2", "season": 1,
                "episode_start": 2, "episode_end": 2, "source": "manual",
            },
        ]
        return {
            **_batch_payload(),
            "work": {"type": None},
            "works": {"series:s-1": first, "series:s-2": second},
            "resource": {
                **_batch_payload()["resource"], "season": None,
                "episode_start": None, "episode_end": None,
                "batch_scope": "franchise",
            },
            "file_associations": {
                "version": 1, "status": "complete", "items": items,
            },
        }

    def test_multi_work_same_target_merges_into_one_plan(self):
        payload = self._multi_work_payload()
        files = [
            DiskFile("/d/A/ep.mkv", 300, "A/ep.mkv"),
            DiskFile("/d/B/ep.mkv", 300, "B/ep.mkv"),
        ]
        lib = _library()
        rule = _rule("all-tv", 10, lib.id, PRESET_TV)
        result = build_plan(payload, files, [rule], [lib])
        destinations = {op.dst for op in result.ops if op.op_type == "move"}
        assert any("作品甲" in path for path in destinations)
        assert any("作品乙" in path for path in destinations)
        assert result.rule is rule

    def test_multi_work_different_targets_is_rejected(self):
        payload = self._multi_work_payload(second_is_anime=False)
        anime = _library("anime", "/anime")
        live = _library("live", "/live")
        rules = [
            _rule("anime", 1, anime.id, PRESET_TV, {
                "field": "series.is_anime", "operator": "eq", "value": True,
            }),
            _rule("live", 2, live.id, PRESET_TV, {
                "field": "series.is_anime", "operator": "eq", "value": False,
            }),
        ]
        files = [
            DiskFile("/d/A/ep.mkv", 300, "A/ep.mkv"),
            DiskFile("/d/B/ep.mkv", 300, "B/ep.mkv"),
        ]
        with pytest.raises(PlanError, match="不同规则、媒体库"):
            build_plan(payload, files, rules, [anime, live])

    def test_authoritative_association_overrides_filename_parse(self):
        payload = _batch_payload()
        payload["file_associations"] = {
            "version": 1,
            "status": "complete",
            "items": [
                {
                    "file_path": f"opaque-{episode}.mkv",
                    "file_size": 300,
                    "work_type": "series",
                    "work_id": payload["work"]["series_id"],
                    "season": 1,
                    "episode_start": episode,
                    "episode_end": episode,
                    "source": "manual",
                }
                for episode in (1, 2, 3)
            ],
        }
        files = [
            DiskFile(f"/d/opaque-{episode}.mkv", 300, f"opaque-{episode}.mkv")
            for episode in (1, 2, 3)
        ]
        lib = _library()
        rule = _rule("tv", 10, lib.id, PRESET_TV)
        result = build_plan(payload, files, [rule], [lib])
        assert len([op for op in result.ops if op.op_type == "move"]) == 3

    def test_non_complete_associations_never_fall_back_to_filename(self):
        payload = _batch_payload()
        payload["file_associations"] = {
            "version": 1, "status": "partial", "items": [],
        }
        lib = _library()
        rule = _rule("tv", 10, lib.id, PRESET_TV)
        with pytest.raises(PlanError, match="请先在计划详情中补全关联"):
            build_plan(payload, _batch_files(), [rule], [lib])

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

    def test_episode_range_derived_from_files(self):
        # episode_start/end 与 work.seasons 皆无 → 期望集由本地文件清单
        # 推导（已解析集同季，min..max 连续区间）。
        payload = _batch_payload()
        payload["resource"] = {
            **payload["resource"],
            "episode_start": None,
            "episode_end": None,
        }
        payload["work"] = {**payload["work"], "seasons": None, "episodes": []}
        lib = _library()
        rule = _rule("tv", 10, lib.id, PRESET_TV)
        result = build_plan(payload, _batch_files(), [rule], [lib])
        moves = {op.src: op.dst for op in result.ops if op.op_type == "move"}
        assert "/Season 01/" in moves["/downloads/complete/gits/GITS.S01E03.1080p.mkv"]

    def test_derived_range_gap_rejected(self):
        # 推导区间中间缺集（E03 解析不出 → 特典 keep）→ 覆盖度不足拒绝。
        payload = _batch_payload()
        payload["resource"] = {
            **payload["resource"],
            "episode_start": None,
            "episode_end": None,
        }
        payload["work"] = {**payload["work"], "seasons": None, "episodes": []}
        lib = _library()
        rule = _rule("tv", 10, lib.id, PRESET_TV)
        files = _batch_files()[:2] + [
            DiskFile("/downloads/complete/gits/GITS.S01E04.1080p.mkv", 300,
                     "GITS.S01E04.1080p.mkv"),
        ]
        with pytest.raises(PlanError, match="覆盖度不足"):
            build_plan(payload, files, [rule], [lib])

    def test_no_basis_and_unparseable_files_rejected(self):
        # 无显式依据且文件清单也推导不出（无已解析集）→ 拒绝。
        payload = _batch_payload()
        payload["resource"] = {
            **payload["resource"],
            "episode_start": None,
            "episode_end": None,
        }
        payload["work"] = {**payload["work"], "seasons": None, "episodes": []}
        lib = _library()
        rule = _rule("tv", 10, lib.id, PRESET_TV)
        files = [
            DiskFile("/d/特典A.mkv", 300, "特典A.mkv"),
            DiskFile("/d/特典B.mkv", 300, "特典B.mkv"),
        ]
        with pytest.raises(PlanError, match="无法校验覆盖度"):
            build_plan(payload, files, [rule], [lib])


class TestBatchRecycleMovedir:
    """合集 + move 计划：目标库配置回收站目录时，剩余文件（keep）随种子
    目录整体移入（movedir op）；未配置（默认）则维持原地保留。"""

    def _lib_with_recycle(self, recycle="/data/recycle"):
        lib = _library("lib-anime", "/data/tv_anime")
        lib.recycle_path = recycle
        return lib

    def _files_with_leftovers(self):
        return _batch_files() + [
            DiskFile("/downloads/complete/gits/特典.mkv", 50, "特典.mkv"),
            DiskFile("/downloads/complete/gits/info.nfo", 1, "info.nfo"),
        ]

    def test_movedir_emitted_with_recycle(self):
        lib = self._lib_with_recycle()
        rule = _rule("anime", 10, lib.id, PRESET_TV)
        result = build_plan(
            _batch_payload(), self._files_with_leftovers(), [rule], [lib],
            source_dir="/downloads/complete/gits",
        )
        movedirs = [op for op in result.ops if op.op_type == "movedir"]
        assert len(movedirs) == 1
        assert movedirs[0].src == "/downloads/complete/gits"
        assert movedirs[0].dst == "/data/recycle/gits"

    def test_no_recycle_keeps_in_place(self):
        lib = _library("lib-anime", "/data/tv_anime")  # recycle_path 缺失
        lib.recycle_path = None
        rule = _rule("anime", 10, lib.id, PRESET_TV)
        result = build_plan(
            _batch_payload(), self._files_with_leftovers(), [rule], [lib],
            source_dir="/downloads/complete/gits",
        )
        assert not [op for op in result.ops if op.op_type == "movedir"]

    def test_hardlink_rule_never_moves_dir(self):
        lib = self._lib_with_recycle()
        rule = _rule("anime", 10, lib.id, PRESET_TV)
        rule.file_op = "hardlink"
        result = build_plan(
            _batch_payload(), self._files_with_leftovers(), [rule], [lib],
            source_dir="/downloads/complete/gits",
        )
        assert not [op for op in result.ops if op.op_type == "movedir"]

    def test_flat_torrent_no_movedir(self):
        """平铺在下载根（source_dir=None）绝不移目录（不扫共享下载根）。"""
        lib = self._lib_with_recycle()
        rule = _rule("anime", 10, lib.id, PRESET_TV)
        result = build_plan(
            _batch_payload(), self._files_with_leftovers(), [rule], [lib],
            source_dir=None,
        )
        assert not [op for op in result.ops if op.op_type == "movedir"]

    def test_no_leftovers_no_movedir(self):
        """全部文件都是正片/字幕（无 keep）时无需回收。"""
        lib = self._lib_with_recycle()
        rule = _rule("anime", 10, lib.id, PRESET_TV)
        result = build_plan(
            _batch_payload(), _batch_files(), [rule], [lib],
            source_dir="/downloads/complete/gits",
        )
        assert not [op for op in result.ops if op.op_type == "movedir"]

    def test_existing_recycle_target_rejected(self, tmp_path):
        """回收站目标目录已存在 → 冲突预检拒绝（绝不覆盖）。"""
        recycle = tmp_path / "recycle"
        (recycle / "gits").mkdir(parents=True)
        lib = self._lib_with_recycle(str(recycle))
        rule = _rule("anime", 10, lib.id, PRESET_TV)
        with pytest.raises(PlanError, match="拒绝覆盖"):
            build_plan(
                _batch_payload(), self._files_with_leftovers(), [rule], [lib],
                source_dir="/downloads/complete/gits",
            )


def _multi_season_payload(seasons=None):
    """多季包：season/episode_start/end 均为 NULL，batch_scope=multi_season。"""
    payload = _batch_payload()
    payload["notification_id"] = "n-4"
    payload["resource"] = {
        **payload["resource"],
        "season": None,
        "episode_start": None,
        "episode_end": None,
        "batch_scope": "multi_season",
    }
    payload["work"] = {
        **payload["work"],
        "seasons": seasons
        if seasons is not None
        else [
            {"season_number": 1, "episode_count": 2},
            {"season_number": 2, "episode_count": 2},
        ],
        "episodes": [],
    }
    return payload


def _multi_season_files(*keys: tuple[int, int], base="/downloads/complete/gits"):
    return [
        DiskFile(
            f"{base}/GITS.S{s:02d}E{e:02d}.1080p.mkv",
            300,
            f"GITS.S{s:02d}E{e:02d}.1080p.mkv",
        )
        for s, e in keys
    ]


class TestMultiSeasonBatch:
    def test_groups_by_parsed_season(self):
        lib = _library("lib-anime", "/data/tv_anime")
        rule = _rule("anime", 10, lib.id, PRESET_TV)
        files = _multi_season_files((1, 1), (1, 2), (2, 1), (2, 2))
        result = build_plan(_multi_season_payload(), files, [rule], [lib])
        moves = {op.src: op.dst for op in result.ops if op.op_type == "move"}
        assert "/Season 01/" in moves["/downloads/complete/gits/GITS.S01E01.1080p.mkv"]
        assert "/Season 02/" in moves["/downloads/complete/gits/GITS.S02E02.1080p.mkv"]

    def test_missing_episode_in_one_season_rejected(self):
        lib = _library()
        rule = _rule("tv", 10, lib.id, PRESET_TV)
        files = _multi_season_files((1, 1), (1, 2), (2, 1))  # 缺 S02E02
        with pytest.raises(PlanError, match="第 2 季覆盖度不足"):
            build_plan(_multi_season_payload(), files, [rule], [lib])

    def test_season_without_episode_data_skips_check(self, caplog):
        # 逐季数据缺第 3 季 → 该季跳过覆盖度校验（warning），不整个拒绝。
        lib = _library()
        rule = _rule("tv", 10, lib.id, PRESET_TV)
        payload = _multi_season_payload(
            seasons=[{"season_number": 1, "episode_count": 2}]
        )
        files = _multi_season_files((1, 1), (1, 2), (3, 1))
        with caplog.at_level("WARNING", logger="app.services.organize_planner"):
            result = build_plan(payload, files, [rule], [lib])
        moves = {op.src: op.dst for op in result.ops if op.op_type == "move"}
        assert "/Season 03/" in moves["/downloads/complete/gits/GITS.S03E01.1080p.mkv"]
        assert any("跳过该季校验" in r.message for r in caplog.records)

    def test_no_season_fallback_to_resource_season(self):
        # 多季包不回退 resource.season：解析不出季号的视频按特典 keep，
        # 而不是被并进某一季。
        lib = _library()
        rule = _rule("tv", 10, lib.id, PRESET_TV)
        payload = _multi_season_payload()
        payload["resource"] = {**payload["resource"], "season": 1}  # 防御：意外有值
        files = _multi_season_files((1, 1), (1, 2), (2, 1), (2, 2)) + [
            DiskFile("/downloads/complete/gits/特典.mkv", 50, "特典.mkv"),
        ]
        result = build_plan(payload, files, [rule], [lib])
        keeps = {op.src for op in result.ops if op.op_type == "keep"}
        assert "/downloads/complete/gits/特典.mkv" in keeps

    def test_season_range_derived_from_files(self):
        # 逐季数据完全缺失 → 每季期望集由本地文件清单推导（min..max）。
        lib = _library()
        rule = _rule("tv", 10, lib.id, PRESET_TV)
        payload = _multi_season_payload(seasons=None)
        payload["work"]["seasons"] = None
        files = _multi_season_files((1, 1), (1, 2), (2, 1), (2, 2))
        result = build_plan(payload, files, [rule], [lib])
        moves = {op.src: op.dst for op in result.ops if op.op_type == "move"}
        assert "/Season 02/" in moves["/downloads/complete/gits/GITS.S02E02.1080p.mkv"]

    def test_derived_season_range_gap_rejected(self):
        # 推导区间中间缺集（S02E02 缺失）→ 该季覆盖度不足拒绝。
        lib = _library()
        rule = _rule("tv", 10, lib.id, PRESET_TV)
        payload = _multi_season_payload(seasons=None)
        payload["work"]["seasons"] = None
        files = _multi_season_files((1, 1), (1, 2), (2, 1), (2, 3))
        with pytest.raises(PlanError, match="第 2 季覆盖度不足"):
            build_plan(payload, files, [rule], [lib])


class TestFranchiseBatch:
    def test_franchise_pack_lands_unclassified(self):
        # franchise 合集包 v1 不自动整理：不落 ops、rule=None（待分类/待人工），
        # 不抛 PlanError（避免每 tick 重试）。
        payload = _batch_payload()
        payload["notification_id"] = "n-5"
        payload["resource"] = {
            **payload["resource"],
            "season": None,
            "episode_start": None,
            "episode_end": None,
            "batch_scope": "franchise",
            "collection": "攻壳机动队（系列）",
        }
        payload["work"] = None  # franchise 资源四作品 FK 全空
        lib = _library()
        rule = _rule("tv", 10, lib.id, PRESET_TV)
        result = build_plan(payload, _batch_files(), [rule], [lib])
        assert result.uncategorized
        assert result.ops == []


class TestMovie:
    def test_bluray_sidecars_are_preserved_for_plex_movie(self):
        lib = _library("lib-movies", "/data/movies", kind="movie")
        rule = _rule("movies", 10, lib.id, PRESET_MOVIE)
        files = [
            DiskFile("/d/disc/movie.mkv", 8000, "disc/movie.mkv"),
            DiskFile("/d/disc/subs/track.eng.forced.sup", 20, "disc/subs/track.eng.forced.sup"),
            DiskFile("/d/disc/subs/track.jpn.idx", 2, "disc/subs/track.jpn.idx"),
            DiskFile("/d/disc/subs/track.jpn.sub", 30, "disc/subs/track.jpn.sub"),
            DiskFile("/d/disc/audio/commentary.dts", 500, "disc/audio/commentary.dts"),
            DiskFile("/d/disc/audio/original.truehd", 1000, "disc/audio/original.truehd"),
        ]
        result = build_plan(HAMNET_PAYLOAD, files, [rule], [lib])
        by_src = {op.src: op for op in result.ops}
        base = "/data/movies/Horror/哈姆奈特 (2025)/哈姆奈特 (2025)"
        assert by_src["/d/disc/subs/track.eng.forced.sup"].dst == f"{base}.en.forced.sup"
        assert by_src["/d/disc/subs/track.jpn.idx"].dst == f"{base}.ja.idx"
        assert by_src["/d/disc/subs/track.jpn.sub"].dst == f"{base}.ja.sub"
        movie_dir = "/data/movies/Horror/哈姆奈特 (2025)/Audio Tracks"
        assert by_src["/d/disc/audio/commentary.dts"].dst == f"{movie_dir}/disc/audio/commentary.dts"
        assert by_src["/d/disc/audio/original.truehd"].dst == f"{movie_dir}/disc/audio/original.truehd"

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

    def test_template_with_category_placeholder_uses_first_genre(self):
        lib = _library("lib-movies", "/data/movies", kind="movie")
        rule = _rule("movies", 10, lib.id, PRESET_MOVIE)
        files = [DiskFile("/d/movie.mkv", 8000, "movie.mkv")]
        result = build_plan(HAMNET_PAYLOAD, files, [rule], [lib])
        assert not result.needs_category
        assert result.rule is rule
        assert result.category == "Horror"
        assert result.ops[0].dst.startswith("/data/movies/Horror/")

    def test_template_with_category_placeholder_unresolved_without_genre(self):
        lib = _library("lib-movies", "/data/movies", kind="movie")
        rule = _rule("movies", 10, lib.id, PRESET_MOVIE)
        files = [DiskFile("/d/movie.mkv", 8000, "movie.mkv")]
        payload = {**HAMNET_PAYLOAD, "work": {**HAMNET_PAYLOAD["work"], "genre": []}}
        result = build_plan(payload, files, [rule], [lib])
        assert result.needs_category
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


class TestContentTypeRuleMatching:
    """Regression: build_filter_context must populate the mutually-exclusive
    work FKs (series_id/movie_id) — filter_engine derives ``content_type``
    from them; without them every rule with a content_type condition
    silently never matches in organize planning."""

    def test_content_type_tv_matches_series_payload(self):
        lib = _library()
        rule = _rule(
            "tv", 10, lib.id, PRESET_TV,
            filter={"field": "content_type", "operator": "eq", "value": "tv"},
        )
        result = build_plan(
            GITS_PAYLOAD, [DiskFile("/d/gits ep04.mkv", 1000, "gits ep04.mkv")],
            [rule], [lib],
        )
        assert result.rule is rule

    def test_content_type_movie_rejects_series_payload(self):
        lib = _library()
        rule = _rule(
            "tv", 10, lib.id, PRESET_TV,
            filter={"field": "content_type", "operator": "eq", "value": "movie"},
        )
        result = build_plan(
            GITS_PAYLOAD, [DiskFile("/d/gits ep04.mkv", 1000, "gits ep04.mkv")],
            [rule], [lib],
        )
        assert result.rule is None  # 不命中 → 待分类

    def test_is_batch_condition_distinguishes_batch(self):
        lib = _library()
        singles_only = _rule(
            "singles", 10, lib.id, PRESET_TV,
            filter={"field": "is_batch", "operator": "eq", "value": False},
        )
        result = build_plan(
            _batch_payload(), _batch_files(), [singles_only], [lib],
        )
        assert result.rule is None  # 合集不命中单集规则
