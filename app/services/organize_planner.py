"""整理规划器（内置整理子系统 organize 的规划层）。

纯函数 + 只读文件 IO（冲突预检的 ``os.path.exists/stat``）：不落库、不访问
数据库、不移动任何文件。输入 = 冻结的通知快照 + 调用方收集的磁盘文件清单
+ 有序规则列表 + Library 集合；输出 = 有序 op 列表，或「待分类」信号
（无规则匹配 → ``rule=None``；模板含 ``{category}`` 但类别未定 →
``needs_category=True``）。分流语义（单集/合集/电影、合集覆盖度校验、
字幕随正片、冲突预检）移植自 vault-organizer ``planner.py``，但库选择与
路径生成改走 OrganizeRule first-match-wins + 命名模板渲染。

**DSL 求值的复用方式**：直接调用
``app.services.filter_engine.evaluate_filter_config``，求值上下文是由通知
快照构造的轻量 adapter（``SimpleNamespace`` 树），**不回查 ORM**。理由：
设计文档规定「规划只依据快照，规划/执行均不读活库 metadata」，快照已
携带规则分流所需的全部 DSL 字段（``is_anime`` / ``genre`` / ``year`` /
``collection`` / ``is_batch`` / ``season`` / ``episode`` / ``resolution``
/ ``container`` / ``subtitle_langs``），构造 adapter 既让规划保持纯快照
语义（可在无 DB 会话处运行），又逐字复用引擎的空值语义与大小写规则，
避免两份求值实现漂移。adapter 的两个适配点：``series.year``/``movie.year``
在引擎中从 ``start_date``/``release_date`` 派生，adapter 用快照 ``year``
构造伪日期；``series.collection``/``movie.collection`` 在引擎中经
``loaded_relation`` 解析 ``title_cn or title_en``，adapter 把快照里的
合集显示名包一层同名命名空间。
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date
from types import SimpleNamespace
from typing import Any

from app.schemas.notification import NotificationPayload
from app.services.filter_engine import evaluate_filter_config
from app.services.organize_parser import (
    FileKind,
    classify,
    detect_subtitle_lang,
    parse_episode,
    parse_season_from_path,
)
from app.services.organize_template import (
    TemplateRenderError,
    map_subtitle_lang,
    render_template,
)


class PlanError(Exception):
    """规划无法安全完成（文件缺失/覆盖度不足/模板缺数据/目标冲突等）。

    上层不落计划，下一 tick 自然重试（vault-organizer webhook 500 重投
    语义在内置语境的等价物）。
    """


@dataclass
class DiskFile:
    """调用方收集的磁盘文件（绝不扫描共享下载根，只扫种子独立目录）。

    ``path`` 为本进程视角绝对路径（daemon → 本进程视角的卷绑定解析由
    调用方在收集时完成，planner 不再做翻译）。
    """

    path: str
    size: int
    rel: str  # 相对种子根目录的路径


@dataclass
class PlanOp:
    op_type: str  # "move" | "keep"
    src: str  # 本进程视角绝对路径（已过卷绑定解析）
    dst: str | None  # keep 为 None；move 为渲染后的本进程视角绝对路径
    size: int
    reason: str = ""


@dataclass
class OrganizePlanResult:
    ops: list[PlanOp] = field(default_factory=list)
    rule: Any | None = None  # 命中的 OrganizeRule（ORM 或同形对象）
    library: Any | None = None  # 命中规则的 Library
    category: str | None = None
    # 模板含 {category} 但类别未定 → 待分类（人工指定后重渲染 op 目标）。
    needs_category: bool = False

    @property
    def uncategorized(self) -> bool:
        """无规则匹配 → 上层落 library_id=null 的待分类计划。"""
        return self.rule is None


# --------------------------------------------- 路径前缀翻译（R2 已替代）


def translate_path(path: str, path_map: Mapping[str, str] | None) -> str:
    """外部视角 → 本进程视角前缀翻译：最长前缀匹配，无命中恒等。

    R1 起下载器路径解析走卷绑定
    （``app.services.volume_service.resolve_downloader_path``），R2 起
    媒体服务器路径解析走绑定表最长前缀匹配
    （``app.services.media_server_service.resolve_server_path``）。本函数
    不再被生产代码调用，保留为纯函数参考实现。
    """
    if not path_map:
        return path
    best: tuple[str, str] | None = None
    for src_prefix, dst_prefix in path_map.items():
        prefix = src_prefix.rstrip("/")
        if path == prefix or path.startswith(prefix + "/"):
            if best is None or len(prefix) > len(best[0]):
                best = (prefix, dst_prefix)
    if best is None:
        return path
    prefix, dst_prefix = best
    suffix = path[len(prefix):]
    return dst_prefix.rstrip("/") + suffix


# ---------------------------------------------------------------- DSL 上下文


def build_filter_context(
    payload: NotificationPayload | Mapping[str, Any],
) -> SimpleNamespace:
    """从通知快照构造 filter_engine 求值用的 FileResource-like adapter。"""
    if not isinstance(payload, NotificationPayload):
        payload = NotificationPayload.model_validate(payload)
    work = payload.work
    resource = payload.resource
    work_type = work.type if work else None

    def _work_ns(kind: str) -> SimpleNamespace:
        assert work is not None
        d = date(work.year, 1, 1) if work.year else None
        collection_ns = (
            SimpleNamespace(title_cn=work.collection, title_en=None)
            if work.collection
            else None
        )
        return SimpleNamespace(
            is_anime=work.is_anime,
            genre=list(work.genre or []),
            rating=None,
            start_date=d if kind == "series" else None,
            release_date=d if kind == "movie" else None,
            collection=collection_ns,
        )

    return SimpleNamespace(
        season=resource.season if resource else None,
        episode=resource.episode if resource else None,
        is_batch=resource.is_batch if resource else False,
        episode_start=resource.episode_start if resource else None,
        episode_end=resource.episode_end if resource else None,
        subtitle_langs=list(resource.subtitle_langs or []) if resource else [],
        resolution=resource.resolution if resource else None,
        container=resource.container if resource else None,
        absolute_episode=None,
        file_size=None,
        # 快照不携带的 FileResource 字段一律 None，走引擎空值语义。
        subtitle_group=None,
        source=None,
        video_codec=None,
        audio_codec=None,
        subtitle_type=None,
        title_cn=None,
        title_en=None,
        search_title=None,
        episode_confidence=None,
        series=_work_ns("series") if work_type == "series" else None,
        movie=_work_ns("movie") if work_type == "movie" else None,
    )


# ---------------------------------------------------------------- 模板上下文


def _episode_title(payload: NotificationPayload, season: int, episode: int) -> str | None:
    work = payload.work
    for ep in (work.episodes or []) if work else []:
        if ep.get("season") == season and ep.get("episode") == episode:
            return ep.get("title") or None
    return None


def _template_context(
    payload: NotificationPayload,
    *,
    season: int | None,
    episode: int | None,
    ext: str,
    category: str | None,
    lang: str | None = None,
) -> dict[str, Any]:
    work = payload.work
    resource = payload.resource
    title = None
    if work:
        title = work.title_cn or work.title_en or work.original_title
    year = (work.year if work else None) or (resource.title_year if resource else None)
    episode_title = (
        _episode_title(payload, season, episode)
        if season is not None and episode is not None
        else None
    )
    return {
        "title": title,
        "title_en": work.title_en if work else None,
        "title_cn": work.title_cn if work else None,
        "original_title": work.original_title if work else None,
        "year": year,
        "season": season,
        "episode": episode,
        "episode_title": episode_title,
        "category": category,
        "collection": work.collection if work else None,
        "resolution": resource.resolution if resource else None,
        "container": resource.container if resource else None,
        "ext": ext,
        "lang": lang,
    }


def _render(root_path: str, template: str, context: Mapping[str, Any]) -> str:
    """渲染模板并拼到 Library.root_path 下；渲染失败 → PlanError。"""
    try:
        rel = render_template(template, context)
    except TemplateRenderError as exc:
        raise PlanError(str(exc)) from exc
    return os.path.join(root_path, *rel.split("/"))


# ---------------------------------------------------------------- 主入口


def build_plan(
    payload: NotificationPayload | Mapping[str, Any],
    disk_files: list[DiskFile],
    rules: Iterable[Any],
    libraries: Mapping[str, Any] | Iterable[Any],
    *,
    category: str | None = None,
) -> OrganizePlanResult:
    """规划一个下载完成通知的整理 ops。

    - ``rules``：OrganizeRule（ORM 或同形对象），任意顺序——本函数按
      ``priority`` 稳定升序 first-match-wins（同优先级保持传入顺序，调用方
      按 ``created_at`` 预排）。
    - ``libraries``：``{id: Library}`` 映射或 Library 可迭代。
    - ``disk_files``：磁盘文件清单，``path`` 须为本进程视角（调用方在
      收集时已过下载器卷绑定解析）。
    - ``category``：电影类别目录（模板含 ``{category}`` 时使用）；None 且
      模板引用 ``{category}`` → 返回 ``needs_category=True`` 的待分类结果。
    """
    if not isinstance(payload, NotificationPayload):
        payload = NotificationPayload.model_validate(payload)
    work = payload.work
    resource = payload.resource

    library_map = (
        dict(libraries)
        if isinstance(libraries, Mapping)
        else {lib.id: lib for lib in libraries}
    )

    # first-match-wins：priority 升序，跳过 disabled，filter 为 null 匹配全部。
    context = build_filter_context(payload)
    matched = None
    for rule in sorted(rules, key=lambda r: r.priority):
        if not rule.enabled:
            continue
        if rule.filter is None or evaluate_filter_config(rule.filter, context):
            matched = rule
            break
    if matched is None:
        return OrganizePlanResult()  # 无规则匹配 → 待分类信号

    library = library_map.get(matched.library_id)
    if library is None:
        raise PlanError(f"规则 {matched.name!r} 指向的 Library 不存在：{matched.library_id}")

    # 目标库未绑定卷（root_path 未解析出 = 待绑定）：不落 ops，由上层落
    # 「待绑定」pending 计划（pending_reason=unbound），补绑定后重渲染。
    if getattr(library, "root_path", None) is None:
        return OrganizePlanResult(rule=matched, library=library, category=category)

    template = matched.path_template
    if "{category}" in template and category is None:
        return OrganizePlanResult(
            rule=matched, library=library, category=None, needs_category=True
        )

    files = list(disk_files)

    work_type = work.type if work else None
    if work_type == "movie":
        ops = _plan_movie(payload, files, library, template, category)
    elif work_type == "series":
        if resource and resource.is_batch:
            ops = _plan_batch(payload, files, library, template, category)
        else:
            ops = _plan_single(payload, files, library, template, category)
    else:
        raise PlanError(f"作品类型缺失或未知：{work_type!r}，无法规划")

    _check_conflicts(ops)
    return OrganizePlanResult(
        ops=ops, rule=matched, library=library, category=category
    )


# ---------------------------------------------------------------- 内部 helpers


def _split(disk_files: list[DiskFile]) -> tuple[list[DiskFile], list[DiskFile], list[DiskFile]]:
    videos = [f for f in disk_files if classify(os.path.basename(f.path)) == FileKind.VIDEO]
    subtitles = [f for f in disk_files if classify(os.path.basename(f.path)) == FileKind.SUBTITLE]
    others = [f for f in disk_files if classify(os.path.basename(f.path)) == FileKind.OTHER]
    return videos, subtitles, others


def _ext_of(path: str) -> str:
    return os.path.splitext(path)[1].lower()


def _keep(f: DiskFile, reason: str) -> PlanOp:
    return PlanOp(op_type="keep", src=f.path, dst=None, size=f.size, reason=reason)


def _check_conflicts(ops: list[PlanOp]) -> None:
    """冲突预检：move 目标已存在且 size 不符 → 拒绝（绝不覆盖）。"""
    for op in ops:
        if op.op_type != "move" or op.dst is None or op.dst == op.src:
            continue
        if os.path.exists(op.dst) and os.path.getsize(op.dst) != op.size:
            raise PlanError(f"目标已存在且大小不符，拒绝覆盖：{op.dst}")


def _subtitle_ops(
    payload: NotificationPayload,
    subtitles: list[DiskFile],
    library: Any,
    template: str,
    category: str | None,
    stem_for: Any,
) -> list[PlanOp]:
    """为一批字幕生成 move ops。

    ``stem_for(f)`` 返回该字幕对应正片的 ``(season, episode, 正片主名路径)``
    （主名路径 = 渲染结果去掉扩展名），或 None（无法匹配集号 → keep）。
    语言识别：文件名标记 → 快照 ``resource.subtitle_langs``（恰好一种时）→
    ``und``；再经 ``library.subtitle_lang_map``（空则内置默认表）映射为
    Plex 后缀；同集同语言第 2 份起追加序号。
    """
    resource = payload.resource
    ops: list[PlanOp] = []
    lang_count: dict[tuple[str, str], int] = {}
    for f in subtitles:
        target = stem_for(f)
        if target is None:
            ops.append(_keep(f, "字幕无法匹配集号，原地保留"))
            continue
        season, episode, main_stem_path = target
        lang = detect_subtitle_lang(os.path.basename(f.path))
        if lang is None:
            langs = list(resource.subtitle_langs or []) if resource else []
            lang = langs[0] if len(langs) == 1 else "und"
        mapped = map_subtitle_lang(lang, library.subtitle_lang_map)
        key = (main_stem_path, mapped)
        lang_count[key] = lang_count.get(key, 0) + 1
        suffix = f".{mapped}"
        if lang_count[key] > 1:
            suffix += f".{lang_count[key]}"
        ops.append(
            PlanOp(
                op_type="move",
                src=f.path,
                dst=f"{main_stem_path}{suffix}{_ext_of(f.path)}",
                size=f.size,
            )
        )
    return ops


def _render_main(
    payload: NotificationPayload,
    library: Any,
    template: str,
    category: str | None,
    f: DiskFile,
    *,
    season: int | None,
    episode: int | None,
) -> tuple[str, str]:
    """渲染正片目标路径，返回 (完整 dst, 去掉扩展名的主名路径)。"""
    ext = _ext_of(f.path)
    dst = _render(
        library.root_path,
        template,
        _template_context(
            payload, season=season, episode=episode, ext=ext, category=category
        ),
    )
    stem = dst[: -len(ext)] if ext and dst.endswith(ext) else os.path.splitext(dst)[0]
    return dst, stem


# ---------------------------------------------------------------- 三种规划


def _plan_single(
    payload: NotificationPayload,
    disk_files: list[DiskFile],
    library: Any,
    template: str,
    category: str | None,
) -> list[PlanOp]:
    resource = payload.resource
    if resource is None or resource.season is None:
        raise PlanError("缺少季号，无法规划单集")
    if resource.episode is None:
        raise PlanError("缺少集号，无法规划单集")

    videos, subtitles, others = _split(disk_files)
    if not videos:
        raise PlanError("下载目录中未找到视频文件")
    videos.sort(key=lambda f: f.size, reverse=True)
    main, extra_videos = videos[0], videos[1:]

    dst, stem = _render_main(
        payload, library, template, category, main,
        season=resource.season, episode=resource.episode,
    )
    ops = [PlanOp(op_type="move", src=main.path, dst=dst, size=main.size)]
    ops += [_keep(f, "非主视频，原地保留") for f in extra_videos]
    ops += _subtitle_ops(
        payload, subtitles, library, template, category,
        lambda f: (resource.season, resource.episode, stem),
    )
    ops += [_keep(f, "非媒体文件，原地保留") for f in others]
    return ops


def _plan_batch(
    payload: NotificationPayload,
    disk_files: list[DiskFile],
    library: Any,
    template: str,
    category: str | None,
) -> list[PlanOp]:
    resource = payload.resource
    work = payload.work

    videos, subtitles, others = _split(disk_files)
    if not videos:
        raise PlanError("下载目录中未找到视频文件")

    def resolve_season(f: DiskFile, parsed: int | None) -> int | None:
        return (
            parsed
            or parse_season_from_path(f.rel)
            or (resource.season if resource else None)
        )

    # 逐文件解析 (season, episode)：文件名 SxxExx → 目录分量 → resource.season
    # 回退链；解析不出的视频按特典 keep。
    episodes: dict[tuple[int, int], DiskFile] = {}
    ops: list[PlanOp] = []
    for f in videos:
        parsed_season, episode = parse_episode(os.path.basename(f.path))
        season = resolve_season(f, parsed_season)
        if season is None or episode is None:
            ops.append(_keep(f, "无法解析集号，按特典原地保留"))
            continue
        key = (season, episode)
        if key in episodes:
            raise PlanError(f"合集内出现重复集号 s{season:02d}e{episode:02d}，拒绝硬猜")
        episodes[key] = f

    # 覆盖度校验：期望集由 episode_start/end 或 work.seasons 展开，
    # 「期望集 ⊆ 已解析集」；缺集 / 无校验依据 → 规划失败，绝不硬猜。
    expected: set[tuple[int, int]]
    if (
        resource is not None
        and resource.episode_start is not None
        and resource.episode_end is not None
    ):
        if resource.season is None:
            raise PlanError("合集缺少季号，无法校验覆盖度")
        expected = {
            (resource.season, e) for e in range(resource.episode_start, resource.episode_end + 1)
        }
    elif work and work.seasons:
        expected = {
            (s["season_number"], e)
            for s in work.seasons
            for e in range(1, s["episode_count"] + 1)
        }
    else:
        raise PlanError("合集缺少集数范围与逐季数据，无法校验覆盖度")
    missing = sorted(expected - episodes.keys())
    if missing:
        desc = ", ".join(f"s{s:02d}e{e:02d}" for s, e in missing[:5])
        raise PlanError(f"合集覆盖度不足，缺 {len(missing)} 集（{desc}），拒绝整理")

    stems: dict[tuple[int, int], str] = {}
    for (season, episode), f in sorted(episodes.items()):
        dst, stem = _render_main(
            payload, library, template, category, f, season=season, episode=episode
        )
        stems[(season, episode)] = stem
        ops.append(PlanOp(op_type="move", src=f.path, dst=dst, size=f.size))

    def subtitle_target(f: DiskFile) -> tuple[int, int, str] | None:
        parsed_season, episode = parse_episode(os.path.basename(f.path))
        season = resolve_season(f, parsed_season)
        if season is None or episode is None or (season, episode) not in episodes:
            return None
        return season, episode, stems[(season, episode)]

    ops += _subtitle_ops(payload, subtitles, library, template, category, subtitle_target)
    ops += [_keep(f, "非媒体文件，原地保留") for f in others]
    return ops


def _plan_movie(
    payload: NotificationPayload,
    disk_files: list[DiskFile],
    library: Any,
    template: str,
    category: str | None,
) -> list[PlanOp]:
    videos, subtitles, others = _split(disk_files)
    if not videos:
        raise PlanError("下载目录中未找到视频文件")
    videos.sort(key=lambda f: f.size, reverse=True)
    main, extra_videos = videos[0], videos[1:]

    dst, stem = _render_main(
        payload, library, template, category, main, season=None, episode=None
    )
    ops = [PlanOp(op_type="move", src=main.path, dst=dst, size=main.size)]
    ops += [_keep(f, "非主视频，原地保留") for f in extra_videos]
    ops += _subtitle_ops(
        payload, subtitles, library, template, category,
        lambda f: (0, 0, stem),
    )
    ops += [_keep(f, "非媒体文件，原地保留") for f in others]
    return ops
