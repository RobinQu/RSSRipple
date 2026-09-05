"""One-off data migration: split legacy series-level TVSeries rows into
per-season works and hang them under a WorkCollection (作品单季化 P5).

Spec: docs/design/per-season-works.md 「迁移方案」节. For every legacy
TVSeries (ascending created_at):

  a. Get-or-create the shell collection: keep an existing ``collection_id``;
     else reverse-lookup the series-level identity in COLLECTION identity
     bags; else normalized base-title get-or-create
     (``external_source="series_group"``, ``external_id NULL`` — the
     franchise-pack get-or-create pattern).
  b. Identity move: bag ids whose registry granularity is series-level
     (wikipedia/tmdb/imdb) are RE-POINTED onto the collection bag. The bag's
     ``UniqueConstraint(source, external_id)`` makes a literal dual-bag copy
     physically impossible, so the row moves; the work's PRIMARY
     ``external_id``/``external_source`` columns stay (creator-wins), which
     keeps the compat-window primary-column lookup channel intact, and the
     season works additionally bag synthetic ``{series_id}#s{N}`` identities.
     Season-granularity bag ids (bangumi/mal/anilist/douban) stay on the
     anchor season work — except when the work's non-batch resources
     unanimously evidence one season ≠ 1, in which case they move to that
     season's work (``_reassign_season_level_identities``, P6).
  c. Season set: ``seasons`` JSON ∪ Episode rows ∪ resource ``season`` /
     ``batch_seasons`` values. Empty or ``{1}`` → single-season path
     (``season_number=1`` + collection attach, no split).
  d. Multi-season split: S1 (or the smallest season) reuses the original row;
     every other season gets a new work (metadata copied, ``start_date`` kept
     on S1 only, season-qualified aliases, synthetic ``{series_id}#s{N}``
     identity when a series-level primary id exists).
  e. Children re-pointed by season: episodes / file_resources /
     resource_file_assignments / resource_work_links / pending_decisions /
     channel_raw_title_mappings. Season-less resources with an
     ``absolute_episode`` are located along the collection members; still
     indeterminate ones are parked on the collection
     (``park_resource_on_collection`` semantics → Channel confirmation).
  f. AgentWork subscriptions are re-targeted by download history: each
     subscription anchored on the reused original row moves to the season
     work where the agent actually completed downloads (max completed tasks,
     ties broken by recency then higher season). Subscriptions without any
     completed history stay on the anchor. The summary lists affected Agents
     and the season works they should additionally subscribe to.
  g. Finalize (apply only): ``backfill_search_text`` + collection
     ``search_text`` fill, then the partial unique index
     ``uq_tv_series_collection_season``.

Idempotent: a re-run skips already-migrated works (collection attached and
no legacy multi-season evidence) and converges instead of duplicating season
works. When two legacy rows of the SAME IP collapse into one collection, the
later row's seasons are adopted by the existing members and the redundant
row is absorbed (bag merge + delete) instead of violating
``(collection_id, season_number)`` uniqueness.

NOTE on locking: the embedded-Turso backend holds a single-process exclusive
file lock, so STOP the app (``docker compose stop app worker``) before running
this against a live database. BACK UP first (pg_dump / copy the db file+wal):
the migration is one-way and irreversible.

Dry-run by default — the full action plan is staged in one transaction and
rolled back at the end; pass --apply to write.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

import app.database as app_database
from app.models.agent import Agent
from app.models.agent_work import AgentWork
from app.models.channel_raw_title_mapping import ChannelRawTitleMapping
from app.models.download_task import DownloadTask
from app.models.episode import Episode
from app.models.file_resource import FileResource
from app.models.movie import Movie
from app.models.pending_decision import PendingDecision
from app.models.resource_file_assignment import ResourceFileAssignment
from app.models.resource_work_link import ResourceWorkLink
from app.models.series import TVSeries
from app.models.work_collection import WorkCollection
from app.models.work_external_id import WorkExternalId
from app.services.external_ids import (
    _bag_match_condition,
    add_external_id,
    find_work_by_external_id,
    list_external_ids,
    merge_external_id_bags,
)
from app.services.fts import backfill_search_text
from app.services.metadata_episode_reconcile import (
    is_unsplit_legacy_series,
    park_resource_on_collection,
    seasons_map_from_list,
)
from app.services.metadata_service import (
    _collection_members,
    _find_collection_by_titles,
    locate_absolute_episode_in_collection,
)
from app.services.metadata_source_registry import (
    REGISTRY_SOURCES,
    canonicalize_external_id,
    granularity_of,
    make_season_identity,
    split_season_identity,
)
from app.services.resource_parser import season_from_title, strip_season_from_title
from app.services.work_search_events import build_search_text

logger = logging.getLogger(__name__)

BATCH_SIZE = 20

SERIES_GROUP_SOURCE = "series_group"

PARTIAL_UNIQUE_INDEX_DDL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_tv_series_collection_season "
    "ON tv_series (collection_id, season_number) WHERE collection_id IS NOT NULL"
)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


@dataclass
class SeasonOutcome:
    season: int
    action: str  # "reuse-original" | "create" | "adopt-existing"
    work_id: str
    episodes: int = 0
    resources: int = 0


@dataclass
class SeriesReport:
    series_id: str
    title: str
    status: str = "split"  # "split" | "single-season" | "skipped" | "absorbed"
    collection_id: str | None = None
    collection_title: str | None = None
    collection_action: str | None = None  # "existing" | "bag-hit" | "title-hit" | "created"
    seasons: list[int] = field(default_factory=list)
    outcomes: list[SeasonOutcome] = field(default_factory=list)
    identities_moved: list[str] = field(default_factory=list)
    synthetic_identities: list[str] = field(default_factory=list)
    parked_resources: list[str] = field(default_factory=list)
    episodes_moved: int = 0
    resources_moved: int = 0
    links_moved: int = 0
    links_created: int = 0
    season_identities_routed: list[str] = field(default_factory=list)
    assignments_moved: int = 0
    decisions_moved: int = 0
    mappings_moved: int = 0
    agent_suggestions: list[dict] = field(default_factory=list)
    subscriptions_retargeted: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Identity helpers
# ---------------------------------------------------------------------------


def _canon_granularity(source: str | None, external_id: str | None) -> tuple[str, str] | None:
    """(canonical_id, "series"|"season") for a registry-shaped id, else None."""
    canon = canonicalize_external_id(external_id, source)
    if not canon:
        return None
    if split_season_identity(canon) is not None:
        return canon, "season"
    prefix = canon.split(":", 1)[0] if ":" in canon else None
    gran = granularity_of(prefix, "tv") if prefix else None
    if gran is None:
        gran = granularity_of(source, "tv")
    if gran not in ("series", "season"):
        return None
    return canon, gran


async def _series_level_ids(db: AsyncSession, series: TVSeries) -> list[tuple[str, str]]:
    """All series-level-granularity ids a legacy work carries (bag + primary)."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for row in await list_external_ids(db, "series", series.id):
        res = _canon_granularity(row.source, row.external_id)
        if res and res[1] == "series" and res[0] not in seen:
            seen.add(res[0])
            out.append((row.source, res[0]))
    res = _canon_granularity(series.external_source, series.external_id)
    if res and res[1] == "series" and res[0] not in seen:
        out.append((series.external_source, res[0]))
    return out


async def _move_series_level_identities(
    db: AsyncSession, series: TVSeries, collection: WorkCollection, report: SeriesReport
) -> None:
    """Move series-level bag ids onto the collection bag.

    The bag's ``UniqueConstraint(source, external_id)`` forbids a literal
    dual-bag copy, so each series-level row is re-pointed
    (``work_type="collection"``) unless another row already owns the id —
    a collision with the SAME collection collapses (dup dropped), a
    collision with a different owner keeps the row on the work and is noted
    as a dedup candidate. The work's primary columns are never touched; a
    series-level primary id is additionally bagged on the collection so
    future series-level matches hit the collection bag (spec step b).
    """
    for row in await list_external_ids(db, "series", series.id):
        res = _canon_granularity(row.source, row.external_id)
        if res is None or res[1] != "series":
            continue  # season-level ids (bangumi/mal/…, synthetic) stay on the work
        canon = res[0]
        other = (
            await db.execute(
                select(WorkExternalId).where(
                    WorkExternalId.source == row.source,
                    _bag_match_condition(row.source, canon),
                    WorkExternalId.id != row.id,
                )
            )
        ).scalars().first()
        if other is not None:
            if other.work_type == "collection" and other.work_id == collection.id:
                await db.delete(row)  # the collection already owns this identity
                report.identities_moved.append(canon)
            else:
                report.notes.append(
                    f"identity {canon} owned by {other.work_type} "
                    f"{other.work_id[:8]} — kept on the work (dedup candidate)"
                )
            continue
        row.work_type = "collection"
        row.work_id = collection.id
        report.identities_moved.append(canon)
    # Primary column (creator-wins, untouched): bag a collection-side copy.
    res = _canon_granularity(series.external_source, series.external_id)
    if res and res[1] == "series":
        if await add_external_id(db, "collection", collection.id, series.external_source, res[0]):
            report.identities_moved.append(res[0])


# ---------------------------------------------------------------------------
# Season-level identity routing (P6 增强)
# ---------------------------------------------------------------------------


async def _consistent_resource_season(db: AsyncSession, series: TVSeries) -> int | None:
    """The single season shared by ALL of the work's non-batch resources.

    Season-less rows and batch packs don't vote (a season pack's ``season``
    describes its own coverage, not the work's identity). Returns None when
    there is no vote or the votes disagree.
    """
    rows = await db.execute(
        select(FileResource.season)
        .where(
            FileResource.series_id == series.id,
            FileResource.is_batch.is_(False),
            FileResource.season.is_not(None),
        )
        .distinct()
    )
    seasons = {s for (s,) in rows if isinstance(s, int)}
    return seasons.pop() if len(seasons) == 1 else None


async def _reassign_season_level_identities(
    db: AsyncSession,
    series: TVSeries,
    work_by_season: dict[int, TVSeries],
    report: SeriesReport,
) -> None:
    """Route season-granularity bag ids onto the season work they describe.

    A legacy series-level row may really be "one season of the IP" (the
    无职转生 accident shape: the merged row's resources all sit in S3 and its
    bag carries the S3 Bangumi entry). The default split keeps every
    season-level id (bangumi/mal/anilist/douban) on the S1 anchor — the wrong
    season work, so a later Bangumi match of the same entry would not bag-hit
    the S3 work. When every non-batch resource linked to the work agrees on
    one season ≠ 1, move its season-level bag ids to that season's work.
    Ambiguous votes, S1-consistent votes, and synthetic ``{id}#s{N}``
    identities (their season is encoded) keep the status quo.
    """
    season = await _consistent_resource_season(db, series)
    if season is None or season == 1:
        return
    target = work_by_season.get(season)
    if target is None or target.id == series.id:
        return
    for row in await list_external_ids(db, "series", series.id):
        res = _canon_granularity(row.source, row.external_id)
        if res is None or res[1] != "season":
            continue
        canon = res[0]
        if split_season_identity(canon) is not None:
            continue  # synthetic identities stay with their encoded season
        other = (
            await db.execute(
                select(WorkExternalId).where(
                    WorkExternalId.source == row.source,
                    _bag_match_condition(row.source, canon),
                    WorkExternalId.id != row.id,
                )
            )
        ).scalars().first()
        if other is not None:
            if other.work_type == "series" and other.work_id == target.id:
                await db.delete(row)  # the season work already owns this id
                report.season_identities_routed.append(canon)
            else:
                report.notes.append(
                    f"season identity {canon} owned by {other.work_type} "
                    f"{other.work_id[:8]} — kept on the anchor (conflict)"
                )
            continue
        row.work_id = target.id
        report.season_identities_routed.append(canon)


# ---------------------------------------------------------------------------
# Collection get-or-create
# ---------------------------------------------------------------------------


def _base_titles(series: TVSeries) -> tuple[str | None, str | None]:
    base_cn = strip_season_from_title(series.title_cn)
    base_en = strip_season_from_title(series.title_en or series.original_title)
    return base_cn, base_en


async def _create_shell_collection(
    db: AsyncSession, series: TVSeries, report: SeriesReport
) -> WorkCollection:
    """Unconditionally create a fresh ``series_group`` shell collection."""
    base_cn, base_en = _base_titles(series)
    aliases: list[str] = []
    for t in (
        series.title_cn,
        series.title_en,
        series.original_title,
        base_cn,
        base_en,
        *(series.aliases or []),
    ):
        if t and t not in aliases:
            aliases.append(t)
    collection = WorkCollection(
        title_cn=(base_cn or base_en or series.original_title or "未命名系列")[:512],
        title_en=base_en,
        aliases=aliases or None,
        external_id=None,
        external_source=SERIES_GROUP_SOURCE,
        poster_url=series.poster_url,
        description=series.description,
    )
    db.add(collection)
    await db.flush()
    report.collection_action = "created"
    return collection


async def _season_member_conflict(
    db: AsyncSession, collection_id: str, season: int, exclude_id: str
) -> TVSeries | None:
    """Another member already holding ``(collection_id, season)``."""
    return (
        await db.execute(
            select(TVSeries).where(
                TVSeries.collection_id == collection_id,
                TVSeries.season_number == season,
                TVSeries.id != exclude_id,
            )
        )
    ).scalars().first()


async def _get_or_create_collection(
    db: AsyncSession, series: TVSeries, report: SeriesReport
) -> WorkCollection:
    """Attach order: existing FK → collection bag hit → normalized base title
    → create a ``series_group`` shell (franchise get-or-create pattern)."""
    if series.collection_id:
        collection = await db.get(WorkCollection, series.collection_id)
        if collection is not None:
            report.collection_action = "existing"
            return collection
        report.notes.append(
            f"collection_id {series.collection_id} dangling — re-resolved by identity/title"
        )
    for source, canon in await _series_level_ids(db, series):
        collection = await find_work_by_external_id(db, "collection", source, canon)
        if collection is not None:
            report.collection_action = "bag-hit"
            return collection
    base_cn, base_en = _base_titles(series)
    titles = [t for t in (base_cn, base_en, series.original_title, *(series.aliases or [])) if t]
    collection = await _find_collection_by_titles(db, titles)
    if collection is not None:
        report.collection_action = "title-hit"
        return collection
    return await _create_shell_collection(db, series, report)


# ---------------------------------------------------------------------------
# Season-set detection
# ---------------------------------------------------------------------------


async def _season_episode_counts(db: AsyncSession, series: TVSeries) -> dict[int, int | None]:
    """``{season: episode_count|None}`` from seasons JSON ∪ Episode rows ∪
    resource season/batch_seasons values (spec step c).

    Every seasons-JSON entry declares its season — an entry whose
    ``episode_count`` is unknown (NULL) still contributes the season itself
    (the 鄉下大叔成為劍聖 shape: S1 declared with NULL count, episodes and
    resources only in S2 — dropping the NULL-count entry would silently lose
    a declared season). Episode counts prefer the JSON value; Episode rows
    only fill seasons the JSON did not count.
    """
    counts: dict[int, int | None] = dict(
        seasons_map_from_list(getattr(series, "seasons", None))
    )
    for entry in getattr(series, "seasons", None) or []:
        if isinstance(entry, dict) and isinstance(entry.get("season_number"), int):
            counts.setdefault(entry["season_number"], None)
    rows = await db.execute(
        select(Episode.season, func.count())
        .where(Episode.series_id == series.id)
        .group_by(Episode.season)
    )
    for season, cnt in rows.all():
        if isinstance(season, int):
            counts.setdefault(season, int(cnt))
    resources = (
        await db.execute(
            select(FileResource.season, FileResource.batch_seasons).where(
                FileResource.series_id == series.id
            )
        )
    ).all()
    for season, batch_seasons in resources:
        values = [season] if season is not None else []
        values += [s for s in (batch_seasons or []) if isinstance(s, int)]
        for value in values:
            counts.setdefault(value, None)
    return counts


def _season_json_entry(series: TVSeries, season: int) -> dict | None:
    for entry in getattr(series, "seasons", None) or []:
        if isinstance(entry, dict) and entry.get("season_number") == season:
            return entry
    return None


def _season_aliases(series: TVSeries, season: int) -> list[str] | None:
    """Season-qualified variants for a new season work: reuse the original
    row's aliases/titles carrying this season's marker, else generate
    「{基础名} 第N季」. Base titles are kept too so title matching converges."""
    variants: list[str] = []
    for t in (series.title_cn, series.title_en, series.original_title, *(series.aliases or [])):
        if t and season_from_title(t) == season and t not in variants:
            variants.append(t)
    if not variants:
        base = (
            strip_season_from_title(series.title_cn)
            or strip_season_from_title(series.title_en)
            or series.original_title
        )
        if base:
            variants.append(f"{base} 第{season}季")
    for t in (series.title_cn, series.title_en, series.original_title):
        if t and t not in variants:
            variants.append(t)
    return variants or None


# ---------------------------------------------------------------------------
# Children re-pointing (spec step e)
# ---------------------------------------------------------------------------


async def _derive_start_dates(
    db: AsyncSession,
    work_by_season: dict[int, TVSeries],
    report: SeriesReport,
) -> None:
    """Fill a split season work's ``start_date`` from its own episodes'
    earliest ``air_date`` when the work has none (P6).

    Only the anchor season inherits the original row's ``start_date``; every
    other season work starts NULL ("待刷新"). A NULL ``start_date`` fails the
    Channel required-field ``year`` gate, which would hold back that season's
    resources until a metadata refresh. Where the DB already carries the
    premiere evidence (episode air dates from the wikipedia/TMDB episode
    lists), derive it offline instead of parking the resources.
    """
    for season, work in sorted(work_by_season.items()):
        if work.start_date is not None:
            continue
        earliest = (
            await db.execute(
                select(func.min(Episode.air_date)).where(
                    Episode.series_id == work.id,
                    Episode.air_date.is_not(None),
                )
            )
        ).scalar_one()
        if earliest is not None:
            work.start_date = earliest
            report.notes.append(
                f"S{season} start_date derived from episode air dates: {earliest}"
            )


async def _retarget_subscriptions_by_history(
    db: AsyncSession,
    work_by_season: dict[int, TVSeries],
    anchor_work: TVSeries,
    report: SeriesReport,
) -> None:
    """Re-point anchor subscriptions at the season work the agent actually
    downloaded.

    Pre-split a subscription targeted the series-level row, which covered ALL
    seasons; the anchor (lowest season) inherits it by convention, but the
    agent's real target is the season its completed downloads landed on
    (production evidence: 无职转生 subscribed S1, downloaded 117× S3).
    Winner = most completed tasks by this agent; ties break by recency, then
    higher season. Subscriptions with no completed history stay on the anchor.
    """
    agent_works = (
        await db.execute(select(AgentWork).where(AgentWork.series_id == anchor_work.id))
    ).scalars().all()
    if not agent_works:
        return
    work_by_id = {w.id: w for w in work_by_season.values()}
    member_ids = list(work_by_id)
    for aw in agent_works:
        rows = (
            await db.execute(
                select(
                    FileResource.series_id,
                    func.count(DownloadTask.id),
                    func.max(DownloadTask.completed_at),
                )
                .join(DownloadTask, DownloadTask.file_resource_id == FileResource.id)
                .where(
                    FileResource.series_id.in_(member_ids),
                    DownloadTask.completed_at.is_not(None),
                    DownloadTask.agent_id == aw.agent_id,
                )
                .group_by(FileResource.series_id)
            )
        ).all()
        if not rows:
            continue
        best_id, best_n, _last = max(
            rows,
            key=lambda r: (r[1], r[2] or datetime.min, work_by_id[r[0]].season_number),
        )
        best = work_by_id.get(best_id)
        if best is None or best.id == anchor_work.id:
            continue
        agent = await db.get(Agent, aw.agent_id)
        report.subscriptions_retargeted.append(
            {
                "agent_id": aw.agent_id,
                "agent_name": agent.name if agent else aw.agent_id,
                "from_season": anchor_work.season_number,
                "to_season": best.season_number,
                "completed_downloads": int(best_n),
            }
        )
        aw.series_id = best.id
    await db.flush()


async def _route_episodes(
    db: AsyncSession,
    series: TVSeries,
    work_by_season: dict[int, TVSeries],
    anchor_work: TVSeries,
    report: SeriesReport,
) -> None:
    episodes = (
        await db.execute(select(Episode).where(Episode.series_id == series.id))
    ).scalars().all()
    for ep in episodes:
        target = work_by_season.get(ep.season, anchor_work)
        if target.id == series.id:
            continue
        collision = (
            await db.execute(
                select(Episode).where(
                    Episode.series_id == target.id,
                    Episode.season == ep.season,
                    Episode.episode == ep.episode,
                )
            )
        ).scalars().first()
        if collision is not None:
            # Adopt-existing path: the target already knows this episode.
            await db.delete(ep)
            report.notes.append(
                f"episode S{ep.season}E{ep.episode} dropped (target already has it)"
            )
            continue
        ep.series_id = target.id
        report.episodes_moved += 1


async def _route_resources(
    db: AsyncSession,
    series: TVSeries,
    collection: WorkCollection,
    work_by_season: dict[int, TVSeries],
    report: SeriesReport,
) -> None:
    resources = (
        await db.execute(select(FileResource).where(FileResource.series_id == series.id))
    ).scalars().all()
    for resource in resources:
        # multi_season packs: per-season links, flat FK cleared (清 FK 仅存 links).
        if getattr(resource, "batch_scope", None) == "multi_season":
            seasons = sorted(
                {s for s in (resource.batch_seasons or []) if s in work_by_season}
            )
            if not seasons and resource.season in work_by_season:
                seasons = [resource.season]
            if len(seasons) == 1:
                # 恰一作品 → 镜像 FK（保 coverage key），与单季包一致。
                resource.series_id = work_by_season[seasons[0]].id
                report.resources_moved += 1
                continue
            if seasons:
                existing = {
                    row.series_id
                    for row in (
                        await db.execute(
                            select(ResourceWorkLink).where(
                                ResourceWorkLink.resource_id == resource.id
                            )
                        )
                    ).scalars().all()
                }
                for season in seasons:
                    work = work_by_season[season]
                    if work.id in existing:
                        continue
                    db.add(
                        ResourceWorkLink(
                            resource_id=resource.id, series_id=work.id, source="auto"
                        )
                    )
                    existing.add(work.id)
                    report.links_created += 1
                resource.series_id = None
                report.resources_moved += 1
                continue
            # Coverage unknown → park below.
        if resource.season is not None and resource.season in work_by_season:
            resource.series_id = work_by_season[resource.season].id
            report.resources_moved += 1
            continue
        if (
            resource.season is None
            and not getattr(resource, "is_batch", False)
            and getattr(resource, "absolute_episode", None) is not None
            and getattr(resource, "episode_confidence", None) != "manual"
        ):
            located = await locate_absolute_episode_in_collection(
                db, collection.id, resource.absolute_episode
            )
            if located is not None:
                member, episode = located
                resource.series_id = member.id
                resource.season = member.season_number
                resource.episode = episode
                resource.episode_confidence = "reconciled"
                report.resources_moved += 1
                continue
        # Still indeterminate → park on the collection (Channel confirmation).
        if resource.collection_id and resource.collection_id != collection.id:
            # A pre-existing (e.g. franchise) collection link wins; just drop
            # the flat work FK.
            resource.series_id = None
            report.notes.append(
                f"resource {resource.id[:8]} kept its existing collection link"
            )
        else:
            park_resource_on_collection(resource, collection)
        report.parked_resources.append(resource.id)


async def _route_assignments(
    db: AsyncSession,
    series: TVSeries,
    work_by_season: dict[int, TVSeries],
    anchor_work: TVSeries,
    report: SeriesReport,
) -> None:
    rows = (
        await db.execute(
            select(ResourceFileAssignment).where(
                ResourceFileAssignment.series_id == series.id
            )
        )
    ).scalars().all()
    for row in rows:
        target = work_by_season.get(row.season, anchor_work) if row.season else anchor_work
        if target.id != series.id:
            row.series_id = target.id
            report.assignments_moved += 1


async def _route_links(
    db: AsyncSession,
    series: TVSeries,
    work_by_season: dict[int, TVSeries],
    anchor_work: TVSeries,
    report: SeriesReport,
) -> None:
    """Re-point links by their resource's season (spec step e).

    ``uq_resource_link_series`` collisions collapse (manual provenance
    transfers onto the surviving row — the ``_repoint_enrichment_rows``
    pattern); a multi_season pack's single legacy link expands into one link
    per covered season work.
    """
    links = (
        await db.execute(
            select(ResourceWorkLink).where(ResourceWorkLink.series_id == series.id)
        )
    ).scalars().all()
    for link in links:
        resource = await db.get(FileResource, link.resource_id)
        seasons: list[int] = []
        if resource is not None:
            if resource.season is not None and resource.season in work_by_season:
                seasons = [resource.season]
            elif getattr(resource, "batch_scope", None) == "multi_season":
                seasons = sorted(
                    {s for s in (resource.batch_seasons or []) if s in work_by_season}
                )
        if not seasons:
            seasons = [anchor_work.season_number]

        async def _place(target: TVSeries, *, reuse_row: bool) -> None:
            existing = (
                await db.execute(
                    select(ResourceWorkLink).where(
                        ResourceWorkLink.resource_id == link.resource_id,
                        ResourceWorkLink.series_id == target.id,
                        ResourceWorkLink.id != link.id,
                    )
                )
            ).scalars().first()
            if existing is not None:
                if link.source == "manual":
                    existing.source = "manual"
                if reuse_row:
                    await db.delete(link)
                return
            if reuse_row:
                link.series_id = target.id
                if target.id != series.id:
                    report.links_moved += 1
            else:
                db.add(
                    ResourceWorkLink(
                        resource_id=link.resource_id,
                        series_id=target.id,
                        source=link.source,
                    )
                )
                report.links_created += 1

        for index, season in enumerate(seasons):
            await _place(work_by_season[season], reuse_row=index == 0)


async def _route_decisions(
    db: AsyncSession,
    series: TVSeries,
    work_by_season: dict[int, TVSeries],
    anchor_work: TVSeries,
    report: SeriesReport,
) -> None:
    rows = (
        await db.execute(
            select(PendingDecision).where(PendingDecision.series_id == series.id)
        )
    ).scalars().all()
    for row in rows:
        target = work_by_season.get(row.season, anchor_work) if row.season else anchor_work
        if target.id != series.id:
            row.series_id = target.id
            report.decisions_moved += 1


async def _route_mappings(
    db: AsyncSession,
    series: TVSeries,
    work_by_season: dict[int, TVSeries],
    anchor_work: TVSeries,
    report: SeriesReport,
) -> None:
    rows = (
        await db.execute(
            select(ChannelRawTitleMapping).where(
                ChannelRawTitleMapping.series_id == series.id
            )
        )
    ).scalars().all()
    for row in rows:
        season = None
        for title in (row.raw_title, row.search_title_key, row.search_title_override):
            season = season_from_title(title)
            if season is not None:
                break
        target = (
            work_by_season.get(season, anchor_work) if season is not None else anchor_work
        )
        if target.id != series.id:
            row.series_id = target.id
            report.mappings_moved += 1


# ---------------------------------------------------------------------------
# Per-series migration
# ---------------------------------------------------------------------------


async def migrate_series(
    db: AsyncSession, series: TVSeries, *, apply: bool = False
) -> SeriesReport:
    """Split one legacy series-level TVSeries into per-season works.

    The full action set is ALWAYS staged in the session (so the returned
    report is accurate for dry-run too); the caller owns the transaction
    boundary — dry-run rolls back at the end, ``--apply`` commits per batch.
    ``apply`` only controls report wording. Idempotent: an already-migrated
    work (collection attached, no legacy multi-season evidence) is skipped.
    """
    report = SeriesReport(
        series_id=series.id,
        title=series.title_cn or series.title_en or series.original_title or series.id,
    )
    if series.collection_id and not is_unsplit_legacy_series(series):
        # season_number is NOT NULL (default 1); 0 = specials and is FALSY —
        # never use ``or 1`` here.
        conflict = await _season_member_conflict(
            db, series.collection_id, series.season_number, series.id
        )
        if conflict is None:
            report.status = "skipped"
            report.collection_id = series.collection_id
            report.notes.append("already per-season / migrated")
            return report
        # Legacy franchise collections group several single-season works at
        # the default season_number=1; the first keeps the slot, later ones
        # re-home onto a fresh shell collection (failure-safe — visible as a
        # manual-merge candidate instead of a duplicate member).
        collection = await _create_shell_collection(db, series, report)
        series.collection_id = collection.id
        await _move_series_level_identities(db, series, collection, report)
        report.collection_id = collection.id
        report.collection_title = collection.display_name
        report.status = "single-season"
        report.notes.append(
            f"collection slot S{series.season_number} already held by "
            f"{conflict.id[:8]} — re-homed to a shell collection "
            "(manual-merge candidate)"
        )
        return report

    # a. collection
    collection = await _get_or_create_collection(db, series, report)
    report.collection_id = collection.id
    report.collection_title = collection.display_name

    # c. season set — computed BEFORE the identity move so a single-season
    # work can still re-home onto a shell collection (step a2) without
    # dragging its series-level ids onto the conflicting collection.
    season_counts = await _season_episode_counts(db, series)
    seasons = sorted(season_counts)
    report.seasons = seasons

    # a2. (collection_id, season_number) uniqueness guard for the
    # single-season path: a title-hit / pre-existing collection may already
    # have an S1 member (legacy franchise collections group several
    # single-season works — real fixture shapes: 零之使魔, 咲-Saki).
    # Re-home onto a fresh shell instead of creating a duplicate member.
    if not seasons or seasons == [1]:
        conflict = await _season_member_conflict(db, collection.id, 1, series.id)
        if conflict is not None:
            collection = await _create_shell_collection(db, series, report)
            report.collection_id = collection.id
            report.collection_title = collection.display_name
            report.notes.append(
                f"collection slot S1 already held by {conflict.id[:8]} "
                f"({(conflict.title_cn or conflict.title_en or '')[:20]!r}) — "
                "re-homed to a shell collection (manual-merge candidate)"
            )

    # b. identity move (series-level bag ids re-pointed onto the collection)
    await _move_series_level_identities(db, series, collection, report)

    res = _canon_granularity(series.external_source, series.external_id)
    series_level_id = res[0] if res and res[1] == "series" else None

    # Single-season path (empty or {1}): attach the shell collection only.
    if not seasons or seasons == [1]:
        series.season_number = 1
        series.collection_id = collection.id
        if seasons == [1]:
            entry = _season_json_entry(series, 1)
            series.seasons = [entry] if entry else None
            if season_counts.get(1) is not None:
                series.number_of_episodes = season_counts[1]
        else:
            series.seasons = None
        series.number_of_seasons = None
        report.status = "single-season"
        if series_level_id:
            synth = make_season_identity(series_level_id, 1)
            prefix = synth.split(":", 1)[0]
            if await add_external_id(db, "series", series.id, prefix, synth):
                report.synthetic_identities.append(synth)
        return report

    # d. multi-season split
    anchor = 1 if 1 in seasons else seasons[0]
    other_members = {
        m.season_number: m
        for m in await _collection_members(db, collection.id)
        if m.id != series.id and isinstance(m.season_number, int)
    }
    work_by_season: dict[int, TVSeries] = {}
    anchor_adopted = False
    for season in seasons:
        if season == anchor and season not in other_members:
            # S1 reuses the original row.
            series.season_number = anchor
            series.collection_id = collection.id
            entry = _season_json_entry(series, anchor)
            series.seasons = [entry] if entry else None
            series.number_of_seasons = None
            if season_counts.get(anchor) is not None:
                series.number_of_episodes = season_counts[anchor]
            work_by_season[season] = series
            report.outcomes.append(
                SeasonOutcome(season=season, action="reuse-original", work_id=series.id)
            )
            continue
        existing = other_members.get(season)
        if existing is not None:
            # Same-IP duplicate legacy row collapsed into this collection:
            # adopt the existing member instead of violating
            # (collection_id, season_number) uniqueness.
            work_by_season[season] = existing
            if season == anchor:
                anchor_adopted = True
            report.outcomes.append(
                SeasonOutcome(season=season, action="adopt-existing", work_id=existing.id)
            )
            report.notes.append(
                f"S{season} already present in collection (work {existing.id[:8]}) — "
                "children routed there; dedup candidate"
            )
            continue
        work = TVSeries(
            title_cn=series.title_cn,
            title_en=series.title_en,
            original_title=series.original_title,
            aliases=_season_aliases(series, season),
            external_id=(
                make_season_identity(series_level_id, season)
                if series_level_id
                and series_level_id.split(":", 1)[0] in REGISTRY_SOURCES
                else None
            ),
            external_source=series.external_source if series_level_id else None,
            description=series.description,
            poster_url=series.poster_url,
            rating=series.rating,
            genre=list(series.genre) if series.genre else None,
            status=series.status,
            is_anime=series.is_anime,
            content_type=series.content_type or "tv",
            number_of_episodes=season_counts.get(season),
            start_date=None,  # S1 keeps the original; later seasons refresh.
            end_date=None,
            season_number=season,
            collection_id=collection.id,
        )
        db.add(work)
        await db.flush()
        if series_level_id:
            synth = make_season_identity(series_level_id, season)
            prefix = synth.split(":", 1)[0]
            if prefix in REGISTRY_SOURCES and await add_external_id(
                db, "series", work.id, prefix, synth
            ):
                report.synthetic_identities.append(synth)
        work_by_season[season] = work
        report.outcomes.append(
            SeasonOutcome(season=season, action="create", work_id=work.id)
        )
    # Bag the synthetic S1 identity on the reused original row too.
    if series_level_id and not anchor_adopted:
        synth = make_season_identity(series_level_id, anchor)
        prefix = synth.split(":", 1)[0]
        if prefix in REGISTRY_SOURCES and await add_external_id(
            db, "series", series.id, prefix, synth
        ):
            report.synthetic_identities.append(synth)

    anchor_work = work_by_season[anchor]

    # b2. Season-level bag ids (bangumi/mal/…) follow the season the work's
    # resources unanimously evidence instead of defaulting to the S1 anchor
    # (无职转生 shape: bangumi:501963 belongs to the S3 work).
    await _reassign_season_level_identities(db, series, work_by_season, report)

    # e. children re-pointing by season
    await _route_episodes(db, series, work_by_season, anchor_work, report)
    await _route_resources(db, series, collection, work_by_season, report)
    await _route_assignments(db, series, work_by_season, anchor_work, report)
    await _route_links(db, series, work_by_season, anchor_work, report)
    await _route_decisions(db, series, work_by_season, anchor_work, report)
    await _route_mappings(db, series, work_by_season, anchor_work, report)

    # e2. Per-season premiere dates from episode air dates (after routing).
    await _derive_start_dates(db, work_by_season, report)

    if anchor_adopted:
        # The original row is fully redundant: re-point its subscriptions at
        # the adopted anchor member, merge the identity bag, delete the row.
        agent_works = (
            await db.execute(select(AgentWork).where(AgentWork.series_id == series.id))
        ).scalars().all()
        for aw in agent_works:
            aw.series_id = anchor_work.id
        await merge_external_id_bags(db, anchor_work, [series])
        await db.flush()
        await db.delete(series)
        await db.flush()
        report.status = "absorbed"

    # f. Subscriptions: re-target anchor subscriptions at the season work the
    # agent actually downloaded (the anchor is the lowest season, which is
    # often NOT the subscribed one — pre-split the row covered all seasons).
    await _retarget_subscriptions_by_history(db, work_by_season, anchor_work, report)

    # Per-season outcome counters for the dry-run action listing
    # (post-routing state — accurate in both modes).
    for outcome in report.outcomes:
        outcome.episodes = int(
            (
                await db.execute(
                    select(func.count())
                    .select_from(Episode)
                    .where(Episode.series_id == outcome.work_id)
                )
            ).scalar_one()
        )
        outcome.resources = int(
            (
                await db.execute(
                    select(func.count())
                    .select_from(FileResource)
                    .where(FileResource.series_id == outcome.work_id)
                )
            ).scalar_one()
        )

    # f (summary): affected agents + suggested extra season subscriptions
    # (seasons of this collection the agent does NOT subscribe after the
    # retarget above; the season a subscription was retargeted AWAY from is
    # deliberately excluded — suggesting it back would be noise).
    member_ids = [w.id for w in work_by_season.values()]
    season_by_work = {w.id: s for s, w in work_by_season.items()}
    affected = (
        await db.execute(select(AgentWork).where(AgentWork.series_id.in_(member_ids)))
    ).scalars().all()
    covered_by_agent: dict[str, set[int]] = {}
    for aw in affected:
        season = season_by_work.get(aw.series_id)
        if season is not None:
            covered_by_agent.setdefault(aw.agent_id, set()).add(season)
    retargeted_from: dict[str, set[int]] = {}
    for r in report.subscriptions_retargeted:
        retargeted_from.setdefault(r["agent_id"], set()).add(r["from_season"])
    for agent_id, covered in covered_by_agent.items():
        skip = retargeted_from.get(agent_id, set())
        missing = [
            w for s, w in sorted(work_by_season.items())
            if s not in covered and s not in skip
        ]
        if missing:
            agent = await db.get(Agent, agent_id)
            report.agent_suggestions.append(
                {
                    "agent_id": agent_id,
                    "agent_name": agent.name if agent else agent_id,
                    "suggested": [
                        {"season": w.season_number, "work_id": w.id} for w in missing
                    ],
                }
            )
    return report


# ---------------------------------------------------------------------------
# Finalization (apply only)
# ---------------------------------------------------------------------------


async def _fill_collection_search_text(db: AsyncSession) -> int:
    """``backfill_search_text`` covers the work tables only; collections get
    the same treatment here (hook-maintained on ORM writes, this heals NULLs)."""
    rows = (
        await db.execute(
            select(WorkCollection).where(WorkCollection.search_text.is_(None))
        )
    ).scalars().all()
    for collection in rows:
        collection.search_text = build_search_text(collection)
    return len(rows)


async def _cleanup_dangling_external_ids(db: AsyncSession) -> int:
    """Delete identity-bag rows whose work no longer exists.

    Pre-existing orphans (works deleted before the bag existed, or by manual
    SQL) are unusable and would fail verify's dangling-FK check — the
    migration is the right moment to collect this garbage.
    """
    removed = 0
    for work_type, model in (
        ("series", TVSeries),
        ("movie", Movie),
        ("collection", WorkCollection),
    ):
        rows = (
            await db.execute(
                select(WorkExternalId).where(WorkExternalId.work_type == work_type)
            )
        ).scalars().all()
        for row in rows:
            if await db.get(model, row.work_id) is None:
                print(f"  [gc] dangling bag row {row.external_id} ({work_type})")
                await db.delete(row)
                removed += 1
    return removed


async def ensure_season_index(db: AsyncSession) -> bool:
    """Create the partial unique index (both backends support partial indexes
    and IF NOT EXISTS). Fails when duplicate (collection, season) members
    remain — run the daily dedup / manual merge first."""
    try:
        await db.execute(text(PARTIAL_UNIQUE_INDEX_DDL))
        await db.commit()
    except Exception as e:  # noqa: BLE001
        await db.rollback()
        print(
            f"  [warn] partial unique index NOT created: {e}\n"
            "         resolve duplicate (collection_id, season_number) members "
            "(dedup/merge) and re-run."
        )
        return False
    return True


async def _print_verification_counts(db: AsyncSession) -> None:
    series_total = (
        await db.execute(select(func.count()).select_from(TVSeries))
    ).scalar_one()
    unattached = (
        await db.execute(
            select(func.count())
            .select_from(TVSeries)
            .where(TVSeries.collection_id.is_(None))
        )
    ).scalar_one()
    dup_rows = await db.execute(
        select(TVSeries.collection_id, TVSeries.season_number, func.count())
        .where(TVSeries.collection_id.is_not(None))
        .group_by(TVSeries.collection_id, TVSeries.season_number)
        .having(func.count() > 1)
    )
    dups = dup_rows.all()
    print(
        f"verify: tv_series={series_total} without-collection={unattached} "
        f"duplicate(collection,season)={len(dups)}"
    )
    if dups:
        for collection_id, season, cnt in dups:
            print(f"  [dup] collection={collection_id[:8]} season={season} count={cnt}")


# ---------------------------------------------------------------------------
# CLI driver
# ---------------------------------------------------------------------------


def _print_series_report(report: SeriesReport, *, apply: bool) -> None:
    verb = "" if apply else "would-"
    title = report.title
    if report.status == "skipped":
        print(f"  [skip] {title!r} — already migrated")
        return
    coll = f"{report.collection_title!r} ({report.collection_action})"
    print(f"  [{report.status}] {title!r} → collection {coll}")
    if report.identities_moved:
        print(f"    身份搬家 → 合集袋: {', '.join(report.identities_moved)}")
    if report.season_identities_routed:
        print(f"    逐季身份 → 对应季作品袋: {', '.join(report.season_identities_routed)}")
    if report.status == "single-season":
        print(f"    单季路径: season_number=1, {verb}挂合集")
    else:
        for outcome in report.outcomes:
            print(
                f"    S{outcome.season}: {verb}{outcome.action} "
                f"work={outcome.work_id[:8]} "
                f"episodes={outcome.episodes} resources={outcome.resources}"
            )
    if report.synthetic_identities:
        print(f"    合成身份: {', '.join(report.synthetic_identities)}")
    print(
        f"    重指向: episodes={report.episodes_moved} resources={report.resources_moved} "
        f"links={report.links_moved}+{report.links_created}new "
        f"assignments={report.assignments_moved} decisions={report.decisions_moved} "
        f"mappings={report.mappings_moved}"
    )
    if report.parked_resources:
        print(
            f"    停泊合集待确认: {len(report.parked_resources)} 资源 "
            f"({', '.join(r[:8] for r in report.parked_resources[:5])}"
            f"{'…' if len(report.parked_resources) > 5 else ''})"
        )
    for retarget in report.subscriptions_retargeted:
        print(
            f"    订阅重指向: Agent {retarget['agent_name']!r} "
            f"S{retarget['from_season']} → S{retarget['to_season']} "
            f"(下载历史 {retarget['completed_downloads']} 次)"
        )
    for suggestion in report.agent_suggestions:
        seasons = ", ".join(f"S{s['season']}" for s in suggestion["suggested"])
        print(
            f"    订阅建议: Agent {suggestion['agent_name']!r} 建议补订 {seasons} 作品"
        )
    for note in report.notes:
        print(f"    note: {note}")


async def run_migration(*, apply: bool, limit: int | None) -> list[SeriesReport]:
    print(f"=== season split migration ({'APPLY' if apply else 'DRY-RUN'}) ===")
    # Module-attribute lookup (not a direct import) so tests that reinstall
    # ``app.database.async_session_factory`` hit the test database.
    async with app_database.async_session_factory() as db:
        query = select(TVSeries).order_by(TVSeries.created_at.asc(), TVSeries.id)
        series_rows = (await db.execute(query)).scalars().all()
        if limit is not None:
            series_rows = series_rows[:limit]
        print(f"{len(series_rows)} tv_series rows to examine")

        reports: list[SeriesReport] = []
        pending = 0
        for index, series in enumerate(series_rows, 1):
            report = await migrate_series(db, series, apply=apply)
            reports.append(report)
            print(f"[{index}/{len(series_rows)}]")
            _print_series_report(report, apply=apply)
            if apply:
                pending += 1
                if pending >= BATCH_SIZE:
                    await db.commit()
                    pending = 0

        if apply:
            await db.commit()
            works_backfilled = await backfill_search_text(db)
            collections_backfilled = await _fill_collection_search_text(db)
            await db.commit()
            print(
                f"search_text backfill: works={works_backfilled} "
                f"collections={collections_backfilled}"
            )
            index_ok = await ensure_season_index(db)
            print(f"partial unique index uq_tv_series_collection_season: {'ok' if index_ok else 'FAILED'}")
            gc = await _cleanup_dangling_external_ids(db)
            await db.commit()
            print(f"dangling identity-bag rows collected: {gc}")
            await _print_verification_counts(db)
        else:
            await db.rollback()

        split = sum(1 for r in reports if r.status == "split")
        single = sum(1 for r in reports if r.status == "single-season")
        skipped = sum(1 for r in reports if r.status == "skipped")
        absorbed = sum(1 for r in reports if r.status == "absorbed")
        created = sum(1 for r in reports if r.collection_action == "created")
        parked = sum(len(r.parked_resources) for r in reports)
        suggestions = sum(len(r.agent_suggestions) for r in reports)
        print(
            f"=== season split migration "
            f"({'applied' if apply else 'dry-run — pass --apply to write'}) ===\n"
            f"series: split={split} single-season={single} skipped={skipped} "
            f"absorbed={absorbed} | collections created={created} | "
            f"parked resources={parked} | agent subscription suggestions={suggestions}"
        )
        return reports


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write changes")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    asyncio.run(run_migration(apply=args.apply, limit=args.limit))


if __name__ == "__main__":
    main()
