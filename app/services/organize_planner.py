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

import logging
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
    detect_subtitle_flags,
    detect_subtitle_lang,
    is_audio,
    parse_season_from_path,
)
from app.services.organize_template import (
    TemplateRenderError,
    map_subtitle_lang,
    render_template,
    sanitize_component,
)
from app.services.torrent_inspect import extract_season_episode_from_path

logger = logging.getLogger(__name__)


class PlanError(Exception):
    """规划无法安全完成（文件缺失/覆盖度不足/模板缺数据/目标冲突等）。

    上层落 ``status=failed`` 的计划行（不抛）：拒绝在变更计划界面可见、
    可人工处置；payload 变化（通知 regenerate）或配置变更
    （replan_open_plans）时自动重建（vault-organizer webhook 500 重投
    语义在内置语境的等价物）。
    """


def _normalized_rel(path: str) -> str:
    return "/".join(part for part in path.replace("\\", "/").split("/") if part)


def _association_for(payload: NotificationPayload, rel: str) -> Any | None:
    associations = payload.file_associations
    if associations is None:
        return None
    wanted = _normalized_rel(rel)
    exact = [a for a in associations.items if _normalized_rel(a.file_path) == wanted]
    if len(exact) == 1:
        return exact[0]
    # Downloader manifests differ on whether the torrent root directory is
    # included. A unique suffix match preserves identity without guessing.
    suffix = [
        a for a in associations.items
        if wanted.endswith("/" + _normalized_rel(a.file_path))
        or _normalized_rel(a.file_path).endswith("/" + wanted)
    ]
    return suffix[0] if len(suffix) == 1 else None


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
        # 作品互斥 FK：filter_engine 据此派生 ``content_type``
        # （series_id→tv、movie_id→movie、audio_work_id→audio），缺失会
        # 让一切 content_type 条件静默不命中。快照 work 段携带真实 id。
        series_id=work.series_id if work else None,
        movie_id=work.movie_id if work else None,
        audio_work_id=None,  # 快照无 audio 作品段（organize 暂不整理音频）
        season=resource.season if resource else None,
        episode=resource.episode if resource else None,
        is_batch=resource.is_batch if resource else False,
        # 资源级合集（franchise 包直挂 WorkCollection）：包一层同名命名
        # 空间，与引擎对 ``series.collection`` 的 loaded_relation 解析同构。
        collection=(
            SimpleNamespace(title_cn=resource.collection, title_en=None)
            if resource and resource.collection
            else None
        ),
        episode_start=resource.episode_start if resource else None,
        episode_end=resource.episode_end if resource else None,
        subtitle_langs=list(resource.subtitle_langs or []) if resource else [],
        subtitle_groups=list(getattr(resource, "subtitle_groups", None) or []) if resource else [],
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
    episode_end: int | None,
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
        "episode_code": (
            f"s{season:02d}e{episode:02d}"
            + (f"-e{episode_end:02d}" if episode_end is not None and episode_end != episode else "")
            if season is not None and episode is not None
            else None
        ),
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
    source_dir: str | None = None,
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
    - ``source_dir``：种子独立目录的本进程视角绝对路径（平铺在下载根或
      单文件种子为 None）。合集 + move 计划且目标库配置了回收站目录时，
      剩余文件随该目录整体移入回收站（movedir op）。
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

    associations = payload.file_associations
    if associations is not None and associations.status != "complete":
        raise PlanError(
            f"文件关联状态为 {associations.status}，请先在计划详情中补全关联"
        )
    if associations is not None:
        work_ids = {item.work_id for item in associations.items}
        if len(work_ids) > 1:
            return _plan_same_target_multi_work(
                payload, disk_files, rules, library_map,
                category=category, source_dir=source_dir,
            )

    # franchise 合集包（跨作品的系列包，经 collection_id 直挂
    # WorkCollection、四作品 FK 全空）：v1 暂不自动整理——成员作品的命名/
    # 库路径需要按成员作品拆分，等待 franchise_service 的成员作品链接功能
    # 落地后再支持。这里直接返回「待分类」信号（rule=None），上层落
    # library_id=null 的 pending 计划（pending_reason=unclassified），
    # 由人工在变更计划界面处理，而不是 PlanError 失败每 tick 重试。
    if resource is not None and resource.batch_scope == "franchise":
        logger.info(
            "[organize] franchise 合集包暂不自动整理，落待人工计划：%s",
            resource.title_raw,
        )
        return OrganizePlanResult()

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
    if "{category}" in template and category is None and work is not None:
        # Metadata genre is already a canonical classification.  Preserve its
        # ordered preference and use the first non-empty tag instead of
        # manufacturing a needless manual-classification plan.
        genres = getattr(work, "genre", None) or []
        category = next(
            (str(value).strip() for value in genres if str(value).strip()),
            None,
        )
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

    # 合集 + move 计划：正片移走后种子目录内的剩余文件（keep 部分）整体
    # 移入目标库配置的回收站目录（movedir）；库未配置回收站（默认）或有
    # 序目录信息（平铺/单文件种子）时保持原地保留不变。hardlink/copy 计划
    # 保种，绝不产生 movedir。
    recycle_path = getattr(library, "recycle_path", None)
    if (
        matched.file_op == "move"
        and work_type == "series"
        and resource is not None
        and resource.is_batch
        and source_dir
        and recycle_path
        and any(op.op_type == "keep" for op in ops)
    ):
        dirname = os.path.basename(source_dir.rstrip("/"))
        ops.append(PlanOp(
            op_type="movedir",
            src=source_dir,
            dst=os.path.join(recycle_path, dirname),
            size=0,
            reason="合集剩余文件移入回收站",
        ))

    _check_conflicts(ops)
    return OrganizePlanResult(
        ops=ops, rule=matched, library=library, category=category
    )


def _plan_same_target_multi_work(
    payload: NotificationPayload,
    disk_files: list[DiskFile],
    rules: Iterable[Any],
    libraries: Mapping[str, Any],
    *,
    category: str | None,
    source_dir: str | None,
) -> OrganizePlanResult:
    """Plan a multi-work resource when every work resolves to one target.

    Each work is planned through the normal single-work ``build_plan`` path;
    merging is allowed only when rule, library, file-op and category agree.
    This deliberately preserves the current one-target OrganizePlan model.
    """
    rule_list = list(rules)
    associations = payload.file_associations
    assert associations is not None and associations.status == "complete"
    groups: dict[str, list[Any]] = {}
    for item in associations.items:
        groups.setdefault(f"{item.work_type}:{item.work_id}", []).append(item)

    group_files: dict[str, list[DiskFile]] = {key: [] for key in groups}
    leftovers: list[DiskFile] = []
    for disk_file in disk_files:
        if classify(os.path.basename(disk_file.path)) == FileKind.VIDEO:
            item = _association_for(payload, disk_file.rel)
            key = f"{item.work_type}:{item.work_id}" if item is not None else None
            if key in group_files:
                group_files[key].append(disk_file)
            else:
                leftovers.append(disk_file)
            continue
        if classify(os.path.basename(disk_file.path)) == FileKind.SUBTITLE:
            season, episode = extract_season_episode_from_path(disk_file.rel)
            candidates = [
                key for key, items in groups.items()
                if episode is not None and any(
                    item.episode_start is not None
                    and item.episode_start <= episode <= (item.episode_end or item.episode_start)
                    and (season is None or item.season == season)
                    for item in items
                )
            ]
            if len(candidates) == 1:
                group_files[candidates[0]].append(disk_file)
            else:
                leftovers.append(disk_file)
            continue
        leftovers.append(disk_file)

    merged_ops: list[PlanOp] = []
    common: OrganizePlanResult | None = None
    for key in sorted(groups):
        work = payload.works.get(key)
        if work is None:
            raise PlanError(f"多作品快照缺少作品元数据：{key}")
        items = groups[key]
        seasons = sorted({item.season for item in items if item.season is not None})
        starts = [item.episode_start for item in items if item.episode_start is not None]
        ends = [
            item.episode_end if item.episode_end is not None else item.episode_start
            for item in items if item.episode_start is not None
        ]
        is_series = work.type == "series"
        is_batch = is_series and len(items) > 1
        child_resource = payload.resource.model_copy(update={
            "is_batch": is_batch,
            "batch_scope": (
                "multi_season" if is_batch and len(seasons) > 1
                else "season" if is_batch else None
            ),
            "season": seasons[0] if len(seasons) == 1 else None,
            "episode": starts[0] if not is_batch and starts else None,
            "episode_start": min(starts) if is_batch and starts else None,
            "episode_end": max(ends) if is_batch and ends else None,
        })
        child = payload.model_copy(update={
            "work": work,
            "resource": child_resource,
            "file_associations": associations.model_copy(update={"items": items}),
        })
        result = build_plan(
            child, group_files[key], rule_list, libraries,
            category=category, source_dir=None,
        )
        if result.rule is None or result.library is None:
            raise PlanError(f"作品 {key} 未命中可执行的整理规则/媒体库")
        if common is None:
            common = result
        elif (
            result.rule.id != common.rule.id
            or result.library.id != common.library.id
            or getattr(result.rule, "file_op", None) != getattr(common.rule, "file_op", None)
            or result.category != common.category
        ):
            raise PlanError("多作品分组命中了不同规则、媒体库、文件操作或类别，无法合并为单一计划")
        merged_ops.extend(result.ops)

    assert common is not None
    merged_ops.extend(_keep(item, "未能唯一归属到作品，原地保留") for item in leftovers)
    _check_conflicts(merged_ops)
    recycle_path = getattr(common.library, "recycle_path", None)
    if (
        getattr(common.rule, "file_op", None) == "move"
        and source_dir and recycle_path
        and any(op.op_type == "keep" for op in merged_ops)
    ):
        merged_ops.append(PlanOp(
            op_type="movedir", src=source_dir,
            dst=os.path.join(recycle_path, os.path.basename(source_dir.rstrip("/"))),
            size=0, reason="多作品合集剩余文件移入回收站",
        ))
    return OrganizePlanResult(
        ops=merged_ops, rule=common.rule, library=common.library,
        category=common.category,
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
    """冲突预检：move 目标已存在且 size 不符 → 拒绝（绝不覆盖）；
    movedir 目标目录已存在 → 拒绝（绝不覆盖）。"""
    for op in ops:
        if op.op_type == "movedir":
            if op.dst is not None and op.dst != op.src and os.path.exists(op.dst):
                raise PlanError(f"回收站目标目录已存在，拒绝覆盖：{op.dst}")
            continue
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
    # Files sharing a source stem (notably VobSub .idx/.sub pairs) must receive
    # the same ordinal so they remain a usable pair after renaming.
    group_number: dict[tuple[str, str, tuple[str, ...], str], int] = {}
    group_count: dict[tuple[str, str, tuple[str, ...]], int] = {}
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
        flags = detect_subtitle_flags(os.path.basename(f.path))
        key = (main_stem_path, mapped, flags)
        source_stem = os.path.splitext(f.rel)[0].casefold()
        group_key = (*key, source_stem)
        if group_key not in group_number:
            group_count[key] = group_count.get(key, 0) + 1
            group_number[group_key] = group_count[key]
        number = group_number[group_key]
        suffix = f".{mapped}"
        if flags:
            suffix += "." + ".".join(flags)
        if number > 1:
            suffix += f".{number}"
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
    episode_end: int | None = None,
) -> tuple[str, str]:
    """渲染正片目标路径，返回 (完整 dst, 去掉扩展名的主名路径)。"""
    ext = _ext_of(f.path)
    dst = _render(
        library.root_path,
        template,
        _template_context(
            payload, season=season, episode=episode, episode_end=episode_end,
            ext=ext, category=category
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
    association = next(
        (
            found
            for f in disk_files
            if classify(os.path.basename(f.path)) == FileKind.VIDEO
            for found in [_association_for(payload, f.rel)]
            if found is not None
        ),
        None,
    )
    if payload.file_associations is not None and association is None:
        raise PlanError("权威文件关联中没有单集主视频映射")
    # A complete file-association snapshot is authoritative for the file
    # mapping, but an auto-generated single-file assignment may carry the
    # episode while still lacking the season. Fall back to the resource-level
    # season rather than treating the nullable association value as an
    # explicit override.
    season = (
        association.season
        if association is not None and association.season is not None
        else (resource.season if resource else None)
    )
    episode = (
        association.episode_start
        if association is not None
        else (resource.episode if resource else None)
    )
    if resource is None or season is None:
        raise PlanError("缺少季号，无法规划单集")
    if episode is None:
        raise PlanError("缺少集号，无法规划单集")

    videos, subtitles, others = _split(disk_files)
    if not videos:
        raise PlanError("下载目录中未找到视频文件")
    videos.sort(key=lambda f: f.size, reverse=True)
    main, extra_videos = videos[0], videos[1:]

    dst, stem = _render_main(
        payload, library, template, category, main,
        season=season, episode=episode,
        episode_end=association.episode_end if association is not None else episode,
    )
    ops = [PlanOp(op_type="move", src=main.path, dst=dst, size=main.size)]
    ops += [_keep(f, "非主视频，原地保留") for f in extra_videos]
    ops += _subtitle_ops(
        payload, subtitles, library, template, category,
        lambda f: (season, episode, stem),
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
    scope = resource.batch_scope if resource else None
    multi_season = scope == "multi_season"

    videos, subtitles, others = _split(disk_files)
    if not videos:
        raise PlanError("下载目录中未找到视频文件")

    def resolve_season(f: DiskFile, parsed: int | None) -> int | None:
        base = parsed or parse_season_from_path(f.rel)
        if multi_season:
            # 多季包：季号只用文件自身解析结果——该 scope 下
            # resource.season 恒为 NULL，即便意外有值也不能回退，否则整包
            # 会被错并进同一季。
            return base
        return base or (resource.season if resource else None)

    # 逐文件解析 (season, episode)：文件名 SxxExx → 目录分量 → resource.season
    # 回退链（多季包无 resource.season 回退，见上）；解析不出的视频按特典 keep。
    episodes: dict[tuple[int, int], DiskFile] = {}
    episode_ends: dict[tuple[int, int], int] = {}
    covered_episodes: dict[tuple[int, int], DiskFile] = {}
    ops: list[PlanOp] = []
    for f in videos:
        association = _association_for(payload, f.rel)
        if association is not None:
            season = association.season
            episode = association.episode_start
            episode_end = association.episode_end or episode
        else:
            if payload.file_associations is not None:
                ops.append(_keep(f, "文件不在权威关联清单中，原地保留"))
                continue
            # Legacy snapshots only: reuse the channel scanner's canonical
            # deterministic parser instead of maintaining organizer regexes.
            parsed_season, episode = extract_season_episode_from_path(f.rel)
            season = resolve_season(f, parsed_season)
            episode_end = episode
        if season is None or episode is None:
            ops.append(_keep(f, "无法解析集号，按特典原地保留"))
            continue
        key = (season, episode)
        run_keys = {(season, value) for value in range(episode, episode_end + 1)}
        overlap = sorted(run_keys & covered_episodes.keys())
        if overlap:
            dup_season, dup_episode = overlap[0]
            raise PlanError(
                f"合集内出现重复集号 s{dup_season:02d}e{dup_episode:02d}，拒绝硬猜"
            )
        episodes[key] = f
        episode_ends[key] = episode_end
        for covered_key in run_keys:
            covered_episodes[covered_key] = f

    # 覆盖度校验：期望集由 episode_start/end 或 work.seasons 展开，
    # 「期望集 ⊆ 已解析集」；缺集 → 规划失败，绝不硬猜。两者皆无时回退到
    # **本地文件清单推导**：全部已解析文件同季 → 期望集 = 该季 min..max
    # 连续区间（中间缺集仍拒绝）；无法推导才视为无校验依据。
    # 多季包按季分组逐组校验（见 _check_multi_season_coverage）。
    if multi_season:
        _check_multi_season_coverage(resource, work, covered_episodes)
    else:
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
            derived = _derive_expected_from_files(covered_episodes)
            if derived is None:
                raise PlanError(
                    "合集缺少集数范围与逐季数据，文件清单亦无法推导，"
                    "无法校验覆盖度"
                )
            expected = derived
        missing = sorted(expected - covered_episodes.keys())
        if missing:
            desc = ", ".join(f"s{s:02d}e{e:02d}" for s, e in missing[:5])
            raise PlanError(f"合集覆盖度不足，缺 {len(missing)} 集（{desc}），拒绝整理")

    stems: dict[tuple[int, int], str] = {}
    for (season, episode), f in sorted(episodes.items()):
        dst, stem = _render_main(
            payload, library, template, category, f, season=season, episode=episode,
            episode_end=episode_ends[(season, episode)],
        )
        stems[(season, episode)] = stem
        ops.append(PlanOp(op_type="move", src=f.path, dst=dst, size=f.size))

    def subtitle_target(f: DiskFile) -> tuple[int, int, str] | None:
        parsed_season, episode = extract_season_episode_from_path(f.rel)
        season = resolve_season(f, parsed_season)
        if season is None or episode is None or (season, episode) not in episodes:
            return None
        return season, episode, stems[(season, episode)]

    ops += _subtitle_ops(payload, subtitles, library, template, category, subtitle_target)
    ops += [_keep(f, "非媒体文件，原地保留") for f in others]
    return ops


def _derive_expected_from_files(
    episodes: dict[tuple[int, int], DiskFile],
) -> set[tuple[int, int]] | None:
    """从本地文件清单推导期望集：全部已解析文件同属一季时，取该季
    min..max 连续区间；无法推导（无已解析集 / 跨季）返回 None。

    推导出的区间会做连续性校验——中间有文件解析不出集号（被当特典
    keep）会形成缺口，按缺集拒绝，与显式依据的语义一致。
    """
    if not episodes:
        return None
    seasons = {s for s, _ in episodes}
    if len(seasons) != 1:
        return None
    season = next(iter(seasons))
    nums = [e for _, e in episodes]
    return {(season, e) for e in range(min(nums), max(nums) + 1)}


def _check_multi_season_coverage(
    resource: Any,
    work: Any,
    episodes: dict[tuple[int, int], DiskFile],
) -> None:
    """多季包按文件解析出的季号分组，逐组做覆盖度校验。

    期望集展开顺序与单季包一致（episode_start/end 优先，回退
    work.seasons 逐季集数）——但 multi_season scope 下
    ``resource.episode_start/end`` 恒为 NULL，实际走的是逐季数据。

    某一季拿不到显式依据（episode_start/end 与 work.seasons 皆无）时
    回退到**本地文件清单推导**（该季 min..max 连续区间，中间缺集仍
    拒绝）；该季已解析集 <2 无法构成区间时才**只记 warning 跳过该季**
    的校验，不整个拒绝——多季包边界信息不全（包内可能只含作品的部分
    季）。拿到期望集的季（显式或推导）保持「缺集拒绝」。
    """
    by_season: dict[int, set[int]] = {}
    for season, episode in episodes:
        by_season.setdefault(season, set()).add(episode)

    for season in sorted(by_season):
        expected_eps: set[int] | None = None
        if (
            resource is not None
            and resource.episode_start is not None
            and resource.episode_end is not None
        ):
            expected_eps = set(
                range(resource.episode_start, resource.episode_end + 1)
            )
        elif work and work.seasons:
            entry = next(
                (s for s in work.seasons if s.get("season_number") == season),
                None,
            )
            if entry and entry.get("episode_count"):
                expected_eps = set(range(1, entry["episode_count"] + 1))
        if expected_eps is None:
            have = sorted(by_season[season])
            if len(have) >= 2:
                # 文件清单推导：该季 min..max 连续区间（中间缺集仍拒绝）。
                expected_eps = set(range(have[0], have[-1] + 1))
            else:
                logger.warning(
                    "[organize] 多季包第 %s 季缺少覆盖度校验依据"
                    "（episode_start/end、逐季数据与文件清单区间皆无），"
                    "跳过该季校验",
                    season,
                )
                continue
        missing = sorted(expected_eps - by_season[season])
        if missing:
            desc = ", ".join(f"e{e:02d}" for e in missing[:5])
            raise PlanError(
                f"合集第 {season} 季覆盖度不足，缺 {len(missing)} 集"
                f"（{desc}），拒绝整理"
            )


def _plan_movie(
    payload: NotificationPayload,
    disk_files: list[DiskFile],
    library: Any,
    template: str,
    category: str | None,
) -> list[PlanOp]:
    videos, subtitles, others = _split(disk_files)
    audio_tracks = [f for f in others if is_audio(os.path.basename(f.path))]
    others = [f for f in others if not is_audio(os.path.basename(f.path))]
    if not videos:
        raise PlanError("下载目录中未找到视频文件")
    if payload.file_associations is not None:
        assigned = [f for f in videos if _association_for(payload, f.rel) is not None]
        if not assigned:
            raise PlanError("权威文件关联中没有电影主文件")
        videos = assigned
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
    # Plex has no documented sidecar-audio naming convention for movies. Keep
    # the tracks losslessly in a clearly scoped child directory of the movie;
    # users can remux them later, while Plex ignores rather than misidentifies
    # them. Preserve relative subdirectories to avoid basename collisions.
    movie_dir = os.path.dirname(stem)
    for f in audio_tracks:
        raw_parts = [part for part in f.rel.replace("\\", "/").split("/") if part]
        if any(part in (".", "..") for part in raw_parts):
            raise PlanError(f"外置音轨包含不安全的相对路径：{f.rel}")
        try:
            rel_parts = [sanitize_component(part) for part in raw_parts]
        except ValueError as exc:
            raise PlanError(f"外置音轨路径无效：{f.rel}") from exc
        audio_rel = os.path.join(*rel_parts) if rel_parts else sanitize_component(
            os.path.basename(f.path)
        )
        ops.append(PlanOp(
            op_type="move",
            src=f.path,
            dst=os.path.join(movie_dir, "Audio Tracks", audio_rel),
            size=f.size,
            reason="Plex 不直接挂载外置音轨，保留供后续 remux",
        ))
    ops += [_keep(f, "非媒体文件，原地保留") for f in others]
    return ops
