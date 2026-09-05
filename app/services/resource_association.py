"""Resource association update service — the edit wizard's write path.

Applies the full desired state from ``PUT /resources/{id}/associations``:
the works set, the batch verdict/scope, the collection link, per-file
placements and generic media-descriptor fields. All invariants downstream
pipelines rely on are enforced here:

- Non-batch resources keep the legacy single-work FK model: at most one
  work; links / assignments / collection cleared.
- Batch resources with exactly one work mirror it into the legacy FK (the
  agent dedup coverage key reads that column); multi-work packs clear the
  legacy FKs and carry associations in ``resource_work_links`` only.
- ``batch_scope`` is derived automatically: all-movie → "movies"; mixed
  tv+movie or multi-tv → "franchise"; a single TV work → "season" when the
  season evidence is unambiguous else "multi_season"; no works → keep the
  current scope or fall back to "franchise".
- Two-level association (per-season works): when a ``collection_id`` is
  submitted, every associated series work must belong to that collection.
- Multi-work packs (>1 associated work) must be complete: every work has at
  least one file placement, every media file of the torrent listing is
  assigned (when the listing is known), and each work's episode run covers
  its expected range (whole-range misses are hard errors, gaps are warnings).
- Every file placement must reference a work of the association set; TV
  placements require a season; episode runs within one (work, season) may
  not overlap (hard error) and gaps surface as warnings.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from app.models.file_resource import FileResource
from app.models.movie import Movie
from app.models.resource_file_assignment import ResourceFileAssignment
from app.models.resource_work_link import ResourceWorkLink
from app.models.series import TVSeries
from app.models.work_collection import WorkCollection
from app.schemas.file_resource import ResourceAssociationUpdateRequest

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# Generic media-descriptor fields correctable in wizard step 3. Applied only
# when explicitly present in ``body.fields``; never touch episode_confidence.
MEDIA_FIELDS = (
    "title_cn",
    "title_en",
    "search_title",
    "resolution",
    "subtitle_group",
    "subtitle_groups",
    "source",
    "video_codec",
    "audio_codec",
    "subtitle_type",
    "container",
    "subtitle_langs",
)


class AssociationValidationError(Exception):
    """User-facing validation failure; the API maps it to 422 VALIDATION_ERROR."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


@dataclass
class AssociationUpdateResult:
    warnings: list[str] = field(default_factory=list)


async def _resolve_works(
    db: AsyncSession, body: ResourceAssociationUpdateRequest
) -> dict[tuple[str, str], TVSeries | Movie]:
    """Resolve existing works and materialize external candidates atomically."""
    resolved: dict[tuple[str, str], TVSeries | Movie] = {}
    candidate_ids: dict[str, str] = {}
    for ref in body.works:
        work_id = ref.work_id
        if ref.candidate is not None:
            data = dict(ref.candidate.metadata)
            data.update({
                "content_type": ref.candidate.content_type,
                "external_source": ref.candidate.identity_source,
                "external_id": ref.candidate.external_id,
                "title_cn": ref.candidate.title_cn,
                "title_en": ref.candidate.title_en,
                "original_title": ref.candidate.original_title,
                "poster_url": ref.candidate.poster_url,
            })
            if ref.work_type == "series":
                from app.services.metadata_service import create_or_update_series_from_external

                obj = await create_or_update_series_from_external(db, data)
                if obj is None:
                    raise AssociationValidationError(
                        "无法确定该剧集候选的季号，请先关联合集下的具体季作品"
                    )
            else:
                from app.services.metadata_service import create_or_update_movie_from_external

                obj = await create_or_update_movie_from_external(db, data)
            work_id = obj.id
            candidate_ids[ref.client_key or ""] = work_id
            ref.work_id = work_id
        if work_id is None:
            raise AssociationValidationError("作品引用缺少 work_id")
        key = (ref.work_type, work_id)
        if key in resolved:
            raise AssociationValidationError("作品关联列表中存在重复项")
        if ref.candidate is None:
            model = TVSeries if ref.work_type == "series" else Movie
            obj = await db.get(model, work_id)
            if obj is None:
                raise AssociationValidationError(
                    f"作品不存在：{ref.work_type} {work_id}"
                )
        resolved[key] = obj
    for assignment in body.assignments:
        if assignment.work_id in candidate_ids:
            assignment.work_id = candidate_ids[assignment.work_id]
    return resolved


def _derive_batch_scope(
    resource: FileResource,
    work_keys: set[tuple[str, str]],
    assignments: list,
) -> str | None:
    """Auto-derive ``batch_scope`` from the final works/placement state."""
    if not resource.is_batch:
        return None
    types = {wt for wt, _ in work_keys}
    if not work_keys:
        return resource.batch_scope or "franchise"
    if types == {"movie"}:
        return "movies"
    if types == {"series"} and len(work_keys) == 1:
        seasons: set[int] = set()
        for a in assignments:
            if a.season is not None:
                seasons.add(a.season)
        seasons.update(resource.batch_seasons or [])
        if resource.season is not None:
            seasons.add(resource.season)
        return "multi_season" if len(seasons) >= 2 else "season"
    return "franchise"


def _validate_assignments(
    body: ResourceAssociationUpdateRequest,
    work_keys: set[tuple[str, str]],
    works_map: dict[tuple[str, str], TVSeries | Movie] | None = None,
    media_files: set[str] | None = None,
) -> list[str]:
    """Cross-check placements against the works set.

    Hard errors (422): duplicate paths, placement referencing a work outside
    the association set, a TV placement without a season, overlapping episode
    runs inside one (work, season). Soft warnings: gaps between runs.

    Multi-work packs (>1 work) additionally enforce file-manifest
    completeness (per docs/design/per-season-works.md): every associated work
    has at least one placement; every media file of the torrent listing is
    assigned (skipped with a warning when the listing is unavailable); each
    series work's placements cover its ``number_of_episodes`` range — a
    whole-range miss is a hard error, head/tail shortfalls are warnings.
    """
    result: list[str] = []
    multi_work = len(work_keys) > 1
    seen_paths: set[str] = set()
    intervals: dict[tuple[str, str, int], list[tuple[int, int, str]]] = defaultdict(list)

    for a in body.assignments:
        path = (a.file_path or "").strip()
        if not path:
            raise AssociationValidationError("文件路径不能为空")
        if path in seen_paths:
            raise AssociationValidationError(f"文件重复映射：{path}")
        seen_paths.add(path)
        if (a.work_type, a.work_id) not in work_keys:
            raise AssociationValidationError(
                f"文件 {path} 关联的作品不在作品关联列表中"
            )
        if a.work_type == "series" and a.season is None:
            raise AssociationValidationError(f"TV 文件必须指定季：{path}")
        lo = a.episode_start if a.episode_start is not None else a.episode_end
        hi = a.episode_end if a.episode_end is not None else a.episode_start
        if a.season is not None and lo is not None and hi is not None:
            intervals[(a.work_type, a.work_id, a.season)].append((lo, hi, path))

    for (work_type, work_id, season), ivs in sorted(intervals.items()):
        ivs.sort()
        prev: tuple[int, int, str] | None = None
        for lo, hi, path in ivs:
            if prev is not None:
                plo, phi, ppath = prev
                if lo <= phi:
                    raise AssociationValidationError(
                        f"第 {season} 季集号区间重叠：{ppath} 与 {path}"
                    )
                if lo > phi + 1:
                    result.append(
                        f"第 {season} 季集号存在断档：{phi + 1}-{lo - 1} 缺失"
                        f"（{ppath} → {path}）"
                    )
            prev = (lo, hi, path)

    if not multi_work:
        return result

    def _work_label(key: tuple[str, str]) -> str:
        work = (works_map or {}).get(key)
        if work is None:
            return f"{key[0]} {key[1]}"
        return work.title_cn or work.title_en or work.original_title or key[1]

    # 1) Every associated work has at least one placement.
    assigned_works = {(a.work_type, a.work_id) for a in body.assignments}
    unassigned = sorted(work_keys - assigned_works)
    if unassigned:
        listing = "、".join(_work_label(key) for key in unassigned)
        raise AssociationValidationError(
            f"多作品合集的每个关联作品都必须有文件指派，缺少：{listing}"
        )

    # 2) Every media file of the torrent listing is assigned (best-effort:
    # skipped with a warning when no listing is available).
    if media_files is None:
        result.append("无法获取 torrent 文件清单，已跳过文件覆盖完整性校验")
    else:
        uncovered = sorted(media_files - seen_paths)
        if uncovered:
            listing = "、".join(uncovered[:5])
            suffix = f" 等 {len(uncovered)} 个" if len(uncovered) > 5 else ""
            raise AssociationValidationError(
                f"多作品合集存在未指派的文件：{listing}{suffix}"
            )

    # 3) Each series work's placements cover its expected episode range
    # (whole-range miss → hard error; head/tail shortfall → warning).
    for key in sorted(work_keys):
        if key[0] != "series":
            continue
        work = (works_map or {}).get(key)
        total = getattr(work, "number_of_episodes", None) if work else None
        if not isinstance(total, int) or total < 1:
            continue
        covered: set[int] = set()
        for (wt, wid, _season), ivs in intervals.items():
            if (wt, wid) != key:
                continue
            for lo, hi, _path in ivs:
                covered.update(range(lo, hi + 1))
        expected = set(range(1, total + 1))
        label = _work_label(key)
        if not covered:
            raise AssociationValidationError(
                f"作品「{label}」的指派区间完全未覆盖其应有集数（1-{total}）"
            )
        missing = sorted(expected - covered)
        if len(missing) == total:
            raise AssociationValidationError(
                f"作品「{label}」的指派区间完全未覆盖其应有集数（1-{total}）"
            )
        head = [e for e in missing if e < min(covered)]
        tail = [e for e in missing if e > max(covered)]
        for group, where in ((head, "开头"), (tail, "结尾")):
            if group:
                desc = ", ".join(f"e{e:02d}" for e in group[:5])
                result.append(
                    f"作品「{label}」的指派区间缺少{where}集数：{desc}"
                )
    return result


def _apply_media_fields(resource: FileResource, fields: dict | None) -> None:
    """Generic media-descriptor corrections (explicit keys only)."""
    if not fields:
        return
    for key in MEDIA_FIELDS:
        if key in fields:
            setattr(resource, key, fields[key])


def _apply_assignments(
    db: AsyncSession,
    resource: FileResource,
    body: ResourceAssociationUpdateRequest,
) -> None:
    """Diff-preserving replace of the file placements.

    The payload is the complete desired state:

    - rows absent from the payload are removed (any provenance);
    - untouched identical placements keep their source (auto/llm);
    - changed or new placements land with ``source="manual"``.
    """
    incoming = {a.file_path: a for a in body.assignments}
    existing = {row.file_path: row for row in resource.file_assignments}

    for path in [p for p in existing if p not in incoming]:
        row = existing.pop(path)
        resource.file_assignments.remove(row)

    for path, a in incoming.items():
        t_series = a.work_id if a.work_type == "series" else None
        t_movie = a.work_id if a.work_type == "movie" else None
        row = existing.get(path)
        unchanged = (
            row is not None
            and row.series_id == t_series
            and row.movie_id == t_movie
            and row.season == a.season
            and row.episode_start == a.episode_start
            and row.episode_end == a.episode_end
        )
        if unchanged:
            continue
        if row is None:
            row = ResourceFileAssignment(resource_id=resource.id, file_path=path)
            resource.file_assignments.append(row)
        row.series_id = t_series
        row.movie_id = t_movie
        if a.file_size is not None:
            row.file_size = a.file_size
        row.season = a.season
        row.episode_start = a.episode_start
        row.episode_end = a.episode_end
        row.source = "manual"


def _apply_work_links(
    resource: FileResource,
    work_keys: set[tuple[str, str]],
) -> None:
    """Apply the desired work-link set without delete/reinsert collisions.

    PostgreSQL may flush INSERTs before orphan DELETEs. Replacing an unchanged
    link with a new ORM object can therefore violate the per-resource/work
    unique constraint. Keep matching rows in place, remove only stale rows,
    and create only genuinely new links.
    """
    existing = {
        (
            "series" if row.series_id is not None else "movie",
            row.series_id or row.movie_id,
        ): row
        for row in resource.work_links
    }

    for key, row in list(existing.items()):
        if key not in work_keys:
            resource.work_links.remove(row)

    for work_type, work_id in sorted(work_keys):
        row = existing.get((work_type, work_id))
        if row is not None:
            row.source = "manual"
            continue
        resource.work_links.append(ResourceWorkLink(
            resource_id=resource.id,
            series_id=work_id if work_type == "series" else None,
            movie_id=work_id if work_type == "movie" else None,
            source="manual",
        ))


async def _known_media_files(resource: FileResource) -> set[str] | None:
    """Best-effort torrent media-file listing for the completeness check.

    Only the local .torrent cache is consulted (PUT must stay fast — no live
    fetch / downloader RPC here; the endpoint re-caches the torrent after
    commit). Returns None when no listing is available.
    """
    cached = getattr(resource, "torrent_file", None)
    if not cached:
        return None
    from pathlib import Path

    if not Path(cached).exists():
        return None
    from app.services.torrent_inspect import analyze_torrent_files, parse_torrent_files

    files = parse_torrent_files(cached)
    if not files:
        return None
    return {row["path"] for row in analyze_torrent_files(files).file_parses}


def _validate_collection_consistency(
    body: ResourceAssociationUpdateRequest,
    works_map: dict[tuple[str, str], TVSeries | Movie],
) -> None:
    """Two-level association (per-season works): when a ``collection_id`` is
    submitted, every associated series work must belong to that collection
    (the wizard picks the collection first, then season works within it)."""
    if body.collection_id is None:
        return
    mismatched = [
        work
        for (work_type, _), work in works_map.items()
        if work_type == "series" and work.collection_id != body.collection_id
    ]
    if mismatched:
        listing = "、".join(
            w.title_cn or w.title_en or w.original_title or w.id
            for w in mismatched
        )
        raise AssociationValidationError(
            f"剧集作品必须属于所选合集（collection_id 不一致）：{listing}"
        )


async def apply_association_update(
    db: AsyncSession,
    resource: FileResource,
    body: ResourceAssociationUpdateRequest,
) -> AssociationUpdateResult:
    """Apply the wizard's full desired state atomically (no commit here)."""
    # Load the enrichment relationships we mutate; on a transient instance
    # refresh fails but collection access stays safe (empty collections).
    try:
        await db.refresh(resource, ["work_links", "file_assignments"])
    except Exception:  # noqa: BLE001
        pass

    # 1) Validate everything BEFORE mutating.
    works_map = await _resolve_works(db, body)
    work_keys = set(works_map)
    if not body.is_batch and len(work_keys) > 1:
        raise AssociationValidationError("非合集资源至多关联一个作品")
    if body.is_batch and body.collection_id is not None:
        if await db.get(WorkCollection, body.collection_id) is None:
            raise AssociationValidationError("作品集不存在")
    _validate_collection_consistency(body, works_map)
    media_files: set[str] | None = None
    if body.is_batch and len(work_keys) > 1:
        media_files = await _known_media_files(resource)
    result = AssociationUpdateResult()
    result.warnings.extend(
        _validate_assignments(
            body, work_keys, works_map=works_map, media_files=media_files
        )
    )

    sent = body.model_fields_set

    # 2) Resource-level flags + association tables.
    resource.is_batch = body.is_batch
    resource.batch_scope = _derive_batch_scope(resource, work_keys, body.assignments)

    if body.is_batch:
        resource.episode = None
        # Exactly ONE work overall mirrors into the legacy FK so the agent
        # dedup coverage key keeps working; multi-work packs clear the FKs
        # and rely on the link table alone.
        series_ids = [wid for wt, wid in work_keys if wt == "series"]
        movie_ids = [wid for wt, wid in work_keys if wt == "movie"]
        single_work = len(work_keys) == 1
        resource.series_id = series_ids[0] if single_work and series_ids else None
        resource.movie_id = movie_ids[0] if single_work and movie_ids else None
        resource.audio_work_id = None
        resource.collection_id = body.collection_id

        seasons = sorted({a.season for a in body.assignments if a.season is not None})
        if seasons:
            resource.batch_seasons = seasons

        # Links replace-set: after a wizard save the association set is
        # human-curated, so provenance becomes 'manual'.
        _apply_work_links(resource, work_keys)

        _apply_assignments(db, resource, body)

        from app.services.batch_content_analysis import compute_season_ranges

        resource.season_ranges = compute_season_ranges(resource)
        if resource.batch_scope == "season":
            season_candidates = seasons or (
                [resource.season] if resource.season is not None else []
            )
            if len(season_candidates) == 1:
                resource.season = season_candidates[0]
                resource.batch_seasons = [season_candidates[0]]
            if len(resource.season_ranges or []) == 1:
                only_range = resource.season_ranges[0]
                resource.episode_start = only_range["episode_start"]
                resource.episode_end = only_range["episode_end"]
            else:
                resource.episode_start = None
                resource.episode_end = None
        elif resource.batch_scope == "multi_season":
            resource.season = None
            resource.episode_start = None
            resource.episode_end = None

        # Marking a resource as 合集 settles any stale ambiguous episode flag.
        if resource.episode_confidence == "ambiguous":
            resource.episode_confidence = "manual"
    else:
        resource.batch_scope = None
        resource.episode_start = None
        resource.episode_end = None
        resource.collection_id = None
        resource.season_ranges = None
        resource.batch_seasons = None
        resource.work_links.clear()
        for row in list(resource.file_assignments):
            await db.delete(row)
            resource.file_assignments.remove(row)

        if "episode" in sent:
            resource.episode = body.episode
        if "season" in sent:
            resource.season = body.season
        if "absolute_episode" in sent:
            resource.absolute_episode = body.absolute_episode
        if sent & {"episode", "season", "absolute_episode"}:
            resource.episode_confidence = "manual"

        if work_keys:
            resource.series_id = next(
                wid for wt, wid in work_keys if wt == "series"
            ) if "series" in {wt for wt, _ in work_keys} else None
            resource.movie_id = next(
                wid for wt, wid in work_keys if wt == "movie"
            ) if "movie" in {wt for wt, _ in work_keys} else None
            resource.audio_work_id = None
        # else: no works requested — leave the existing single-work / audio
        # linkage untouched (a media-fields-only save must not unlink).

    # 3) Generic media fields (explicit keys only).
    _apply_media_fields(resource, body.fields)

    logger.info(
        "[association] resource %s updated: batch=%s scope=%s works=%d files=%d",
        resource.id, body.is_batch, resource.batch_scope,
        len(work_keys), len(body.assignments),
    )
    return result
