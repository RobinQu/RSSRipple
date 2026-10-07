"""One-time deduplication of TVSeries / Movie rows created before the
canonical-external-id upsert was in place.

Historically, Exa Agent Search returned the same TMDB id in inconsistent shapes
across successive fetches (e.g. ``TMDB:82684``, ``TMDB 82684``,
``TMDB TV 82684 / season 4``). The old
``create_or_update_series_from_external`` looked up rows by the raw
``external_id`` string, so every new shape spawned a new TVSeries entity for
the same real work. This module merges those duplicate rows into a canonical
survivor and re-points all references (FileResource / ChannelRawTitleMapping /
AgentWork / PendingDecision / Episode / ResourceWorkLink /
ResourceFileAssignment) at the survivor.

Grouping key: ``(normalized_title_cn, normalized_title_en)`` — both compared
after ``normalize_title`` so trad/simp Chinese and half/full-width variants
collapse. Rows are only grouped when at least one of the titles is non-empty.
Survivor within a group: the entity with the smallest ``created_at`` (ties
broken by id) — keeps the oldest row so historical references stay valid.

Idempotent: running it twice does nothing on the second pass.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_work import AgentWork
from app.models.channel_raw_title_mapping import ChannelRawTitleMapping
from app.models.episode import Episode
from app.models.file_resource import FileResource
from app.models.movie import Movie
from app.models.pending_decision import PendingDecision
from app.models.resource_file_assignment import ResourceFileAssignment
from app.models.resource_work_link import ResourceWorkLink
from app.models.series import TVSeries
from app.services.dedup_metadata_policy import (
    DedupConflictError,
    apply_metadata,
    primary_identity_owner,
    require_protected_values,
    select_survivor,
)
from app.services.dedup_work_lock import lock_merge_works
from app.services.external_ids import add_external_id, merge_external_id_bags
from app.services.metadata_episode_reconcile import is_unsplit_legacy_series
from app.services.metadata_service import canonicalize_external_id
from app.services.text_normalizer import normalize_title

logger = logging.getLogger(__name__)


async def _repoint_enrichment_rows(
    db: AsyncSession,
    *,
    src_series_ids: Iterable[str] = (),
    src_movie_ids: Iterable[str] = (),
    dst_series_id: str | None = None,
    dst_movie_id: str | None = None,
) -> tuple[int, int]:
    """Re-point ``resource_work_links`` / ``resource_file_assignments`` rows
    from merged-away works onto the survivor.

    Without this, deleting a duplicate work would silently drop its enrichment
    rows via the FK CASCADE — losing human-curated (``manual``) file mappings.

    Unique-key collisions collapse instead of erroring: a link whose
    ``(resource_id, target)`` pair already exists is dropped, and an
    assignment whose ``(resource_id, file_path)`` row already exists is
    dropped likewise; ``manual`` provenance is preserved onto the surviving
    row. Cross-type re-points onto a movie clear the TV placement fields
    (mirroring :func:`rehome_series_as_movie`).

    Returns ``(links_repointed, assignments_repointed)``.
    """
    assert (dst_series_id is None) != (dst_movie_id is None)
    dst_id = dst_series_id or dst_movie_id
    links_repointed = 0
    for column, ids in (
        (ResourceWorkLink.series_id, list(src_series_ids)),
        (ResourceWorkLink.movie_id, list(src_movie_ids)),
    ):
        if not ids:
            continue
        for link in (await db.execute(
            select(ResourceWorkLink).where(column.in_(ids))
        )).scalars().all():
            dst_column = (
                ResourceWorkLink.series_id if dst_series_id
                else ResourceWorkLink.movie_id
            )
            existing = (await db.execute(
                select(ResourceWorkLink).where(
                    ResourceWorkLink.resource_id == link.resource_id,
                    dst_column == dst_id,
                )
            )).scalars().first()
            if existing is not None:
                if link.source == "manual":
                    existing.source = "manual"
                await db.delete(link)
            else:
                link.series_id = dst_series_id
                link.movie_id = dst_movie_id
                links_repointed += 1

    assignments_repointed = 0
    for column, ids in (
        (ResourceFileAssignment.series_id, list(src_series_ids)),
        (ResourceFileAssignment.movie_id, list(src_movie_ids)),
    ):
        if not ids:
            continue
        for row in (await db.execute(
            select(ResourceFileAssignment).where(column.in_(ids))
        )).scalars().all():
            # uq (resource_id, file_path): a pre-existing row for the same
            # file wins; manual provenance transfers onto it.
            existing = (await db.execute(
                select(ResourceFileAssignment).where(
                    ResourceFileAssignment.resource_id == row.resource_id,
                    ResourceFileAssignment.file_path == row.file_path,
                    ResourceFileAssignment.id != row.id,
                )
            )).scalars().first()
            if existing is not None:
                if row.source == "manual":
                    existing.source = "manual"
                await db.delete(row)
                continue
            row.series_id = dst_series_id
            row.movie_id = dst_movie_id
            if dst_movie_id is not None and column is ResourceFileAssignment.series_id:
                # Cross-type (series → movie): TV placement fields are
                # meaningless on a movie assignment.
                row.season = None
                row.episode_start = None
                row.episode_end = None
            assignments_repointed += 1
    return links_repointed, assignments_repointed


async def rehome_series_as_movie(
    db: AsyncSession, series: TVSeries, movie: Movie
) -> None:
    """Move all references from a proven misfiled TVSeries into a Movie.

    This is the targeted online counterpart of :func:`merge_cross_type_duplicates`.
    Eligibility (the source row says ``content_type='movie'`` and has no
    episode evidence) is deliberately checked by the caller, while this
    function owns the canonical reference migration and identity-bag merge.
    """
    await lock_merge_works(db, [series, movie])
    protected = require_protected_values([series, movie], movie)

    from app.services.decision_rekey import lock_work_choice_agents, rekey_agent_choices

    choice_agents = await lock_work_choice_agents(db, [("series", [series.id]), ("movie", [movie.id])])
    # Multi-work links / per-file assignments follow the work, collapsing
    # per-target / per-file uniqueness collisions (manual provenance wins).
    await _repoint_enrichment_rows(
        db, src_series_ids=[series.id], dst_movie_id=movie.id
    )
    await db.execute(
        update(FileResource)
        .where(FileResource.series_id == series.id)
        .values(series_id=None, movie_id=movie.id, episode_confidence=None)
    )
    await db.execute(
        update(AgentWork)
        .where(AgentWork.series_id == series.id)
        .values(series_id=None, movie_id=movie.id, content_type="movie")
    )
    await db.execute(
        update(ChannelRawTitleMapping)
        .where(ChannelRawTitleMapping.series_id == series.id)
        .values(series_id=None, movie_id=movie.id, content_type="movie")
    )
    await db.execute(
        update(PendingDecision)
        .where(PendingDecision.series_id == series.id)
        .values(series_id=None, movie_id=movie.id)
    )
    await rekey_agent_choices(db, choice_agents)
    await _merge_work_metadata(db, movie, [series], protected)
    await db.flush()
    await db.delete(series)
    await db.flush()
    logger.warning(
        "[metadata] repaired misfiled TVSeries %s (%r) as Movie %s",
        series.id, series.title_cn or series.title_en, movie.id,
    )


@dataclass
class DedupReport:
    series_groups: int = 0
    series_removed: int = 0
    movie_groups: int = 0
    movies_removed: int = 0
    cross_type_merges: int = 0
    file_resources_updated: int = 0
    agent_works_updated: int = 0
    mappings_updated: int = 0
    pending_decisions_updated: int = 0
    episodes_updated: int = 0
    work_links_updated: int = 0
    file_assignments_updated: int = 0
    notes: list[str] = field(default_factory=list)


def _title_keys(entity: TVSeries | Movie) -> set[str]:
    """Normalized title keys used to link related entities together.

    An entity may be reachable via any of ``title_cn`` / ``title_en`` /
    ``original_title`` **or any alias**; two entities are considered the same
    work if they share any of these keys (union-find style). Including
    aliases is essential: metadata agents routinely accumulate aliases that
    span the alternate title formats another row was created under (e.g.
    ``"第四季"`` vs ``"第 4 季"`` with a colon), so without aliases two rows
    for the same real series never cluster.
    """
    keys: set[str] = set()
    raw_titles: list[str | None] = [entity.title_cn, entity.title_en, entity.original_title]
    raw_titles.extend(entity.aliases or [])
    for raw in raw_titles:
        n = normalize_title(raw)
        if n:
            keys.add(n)
    return keys


def _year_conflict(dates: Iterable) -> bool:
    """True when the dated rows of a cluster span more than one year.

    A shared normalized title is NOT proof of identity across very different
    premier years — remakes/reboots/同名系列 (e.g. the 1995 film vs the 2026
    TV series of one franchise) would otherwise be folded into the oldest
    row. ±1 year of slack covers the same work dated slightly differently by
    different sources (December/January premieres). Rows without a date never
    count as evidence either way.
    """
    years = [d.year for d in dates if d is not None]
    return bool(years) and max(years) - min(years) > 1


class _UnionFind:
    """Minimal union-find keyed by entity id."""

    def __init__(self) -> None:
        self._parent: dict[str, str] = {}

    def add(self, x: str) -> None:
        self._parent.setdefault(x, x)

    def find(self, x: str) -> str:
        parent = self._parent
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[rb] = ra

    def groups(self) -> dict[str, list[str]]:
        result: dict[str, list[str]] = {}
        for x in list(self._parent):
            result.setdefault(self.find(x), []).append(x)
        return result


def _cluster_by_shared_title(entities: list, bucket_of=None) -> list[list]:
    """Group ``entities`` so that any two sharing a normalized title end up in
    the same cluster.

    ``bucket_of`` optionally maps an entity to a cluster-isolation key: two
    entities only ever union when they share a title key AND land in the same
    bucket. Per-season works use this to keep different seasons of one IP
    apart no matter how alike their titles/aliases are.
    """
    uf = _UnionFind()
    keyed: dict[tuple, list[str]] = {}
    id_to_entity = {e.id: e for e in entities}
    for e in entities:
        uf.add(e.id)
        bucket = bucket_of(e) if bucket_of is not None else None
        for k in _title_keys(e):
            keyed.setdefault((bucket, k), []).append(e.id)
    for ids in keyed.values():
        first = ids[0]
        for other in ids[1:]:
            uf.union(first, other)
    return [[id_to_entity[i] for i in ids] for ids in uf.groups().values()]


def _series_cluster_bucket(series: TVSeries) -> tuple:
    """Cluster-isolation key for per-season works.

    Only works representing the SAME season may cluster (different seasons of
    one IP inevitably share normalized titles/aliases yet must never merge).
    Legacy unsplit series-level rows (pre-migration, multi-season evidence in
    the inert ``seasons``/``number_of_seasons`` columns) cluster only among
    themselves — never bridged into a per-season row.
    """
    if is_unsplit_legacy_series(series):
        return ("legacy",)
    return ("season", series.season_number if series.season_number is not None else 1)


def _pick_canonical_external_id(candidates: Iterable[TVSeries | Movie]) -> str | None:
    """Prefer already-canonical ``tmdb:<digits>`` / ``imdb:<tt…>`` forms; else
    take the shortest non-empty external_id from the group."""
    best: str | None = None
    for c in candidates:
        canon = canonicalize_external_id(c.external_id, c.external_source, c.content_type)
        if canon and canon.startswith(("tmdb:", "imdb:")):
            return canon
        if canon and (best is None or len(canon) < len(best)):
            best = canon
        elif c.external_id and (best is None or len(c.external_id) < len(best)):
            best = c.external_id
    return best


def _merge_aliases(entities: Iterable[TVSeries | Movie]) -> list[str] | None:
    seen: list[str] = []
    for e in entities:
        for t in (e.title_cn, e.title_en, e.original_title, *(e.aliases or [])):
            if t and t not in seen:
                seen.append(t)
    return seen or None


async def _repoint_series_children(
    db: AsyncSession, dup_ids: list[str], survivor_id: str, report: DedupReport
) -> None:
    """Re-point every series-FK child row at the survivor.

    Rows whose natural key the survivor already owns would violate a unique
    constraint (episodes, channel_raw_title_mappings) or the app-level
    singleton invariants (agent_works) if blindly
    re-pointed — those duplicates are deleted instead (the survivor's row is
    the richer/original one; ambiguous resources re-surface via the normal
    agent-run upsert). Pending decisions are preserved and rekeyed after
    their resource associations have moved.
    """
    from app.services.decision_rekey import lock_work_choice_agents, rekey_agent_choices

    choice_agents = await lock_work_choice_agents(db, [("series", [survivor_id, *dup_ids])])
    n = (await db.execute(
        update(FileResource)
        .where(FileResource.series_id.in_(dup_ids))
        .values(series_id=survivor_id)
    )).rowcount or 0
    report.file_resources_updated += n

    # AgentWork: app-level one-row-per-(agent, work) — drop collisions.
    survivor_agents = {
        aw.agent_id
        for aw in (await db.execute(
            select(AgentWork).where(AgentWork.series_id == survivor_id)
        )).scalars().all()
    }
    n = 0
    for aw in (await db.execute(
        select(AgentWork).where(AgentWork.series_id.in_(dup_ids))
    )).scalars().all():
        if aw.agent_id in survivor_agents:
            await db.delete(aw)
        else:
            aw.series_id = survivor_id
            survivor_agents.add(aw.agent_id)
            n += 1
    report.agent_works_updated += n

    # ChannelRawTitleMapping: uq (channel_id, search_title_key) is series-
    # independent, so re-pointing can never collide — bulk update is safe.
    n = (await db.execute(
        update(ChannelRawTitleMapping)
        .where(ChannelRawTitleMapping.series_id.in_(dup_ids))
        .values(series_id=survivor_id)
    )).rowcount or 0
    report.mappings_updated += n

    # Preserve history rows; pending coverage is rekeyed after associations move.
    n = (await db.execute(update(PendingDecision).where(
        PendingDecision.series_id.in_(dup_ids)
    ).values(series_id=survivor_id))).rowcount or 0
    report.pending_decisions_updated += n

    # Episode: uq (series_id, season, episode) — drop collisions.
    survivor_episodes = {
        (e.season, e.episode)
        for e in (await db.execute(
            select(Episode).where(Episode.series_id == survivor_id)
        )).scalars().all()
    }
    n = 0
    for e in (await db.execute(
        select(Episode).where(Episode.series_id.in_(dup_ids))
    )).scalars().all():
        if (e.season, e.episode) in survivor_episodes:
            await db.delete(e)
        else:
            e.series_id = survivor_id
            survivor_episodes.add((e.season, e.episode))
            n += 1
    report.episodes_updated += n

    # Multi-work links / per-file assignments follow the survivor (their FKs
    # CASCADE on work deletion — without re-pointing, manual mappings would be
    # silently lost).
    links_n, assignments_n = await _repoint_enrichment_rows(
        db, src_series_ids=dup_ids, dst_series_id=survivor_id
    )
    report.work_links_updated += links_n
    report.file_assignments_updated += assignments_n
    await rekey_agent_choices(db, choice_agents)


def _group_still_matches(rows):
    series = isinstance(rows[0], TVSeries)
    groups = _cluster_by_shared_title(rows, bucket_of=_series_cluster_bucket if series else None)
    dates = [row.start_date if series else row.release_date for row in rows]
    return len(groups) == 1 and not _year_conflict(dates)


def _same_cross_type_work(movie, series, movie_keys, series_keys):
    movie_id = canonicalize_external_id(movie.external_id, movie.external_source, "movie")
    series_id = canonicalize_external_id(series.external_id, series.external_source, "tv")
    if (movie_id and series_id and movie_id == series_id) or (
        movie.external_id and movie.external_id == series.external_id
    ):
        return True
    return bool(movie_keys & series_keys) and not _year_conflict([movie.release_date, series.start_date])


async def _merge_work_metadata(db, survivor, duplicates, protected):
    rows = [survivor, *duplicates]
    owner = primary_identity_owner(rows, survivor, protected)
    identity = owner.external_id if "external_id" in protected else _pick_canonical_external_id([owner])
    source = owner.external_source
    aliases = _merge_aliases(rows) if "aliases" not in protected else protected["aliases"]
    if (identity, source) != (survivor.external_id, survivor.external_source):
        from app.services.metadata_source_registry import REGISTRY_SOURCES

        old_identity = _pick_canonical_external_id([survivor])
        old_source = survivor.external_source
        if old_source not in REGISTRY_SOURCES and old_identity:
            old_source = old_identity.split(":", 1)[0]
        await add_external_id(db, "movie" if isinstance(survivor, Movie) else "series",
                              survivor.id, old_source, old_identity)
    apply_metadata(survivor, rows, protected)
    survivor.external_id, survivor.external_source = identity, source
    survivor.aliases = aliases
    await merge_external_id_bags(db, survivor, duplicates)
    # Completeness-based selection can keep a legacy row whose primary id
    # has never been bagged. Keep that id reachable through the bag as well;
    # add_external_id retains the shared no-steal policy for other owners.
    await add_external_id(db, "movie" if isinstance(survivor, Movie) else "series",
                          survivor.id, source, identity)


async def _merge_series_group(
    db: AsyncSession,
    rows: list[TVSeries],
    report: DedupReport,
    survivor: TVSeries | None = None,
    *,
    automatic: bool = False,
    allow_season_change: bool = False,
) -> None:
    if len(rows) < 2:
        return
    await lock_merge_works(db, rows)
    if automatic and not _group_still_matches(rows):
        report.notes.append("[candidate-changed] " + ",".join(sorted(row.id for row in rows)))
        return
    if not allow_season_change and len({row.season_number for row in rows}) > 1:
        raise DedupConflictError(rows, ["season_number"])
    if survivor is None:
        survivor = select_survivor(rows)
    protected = require_protected_values(rows, survivor)
    duplicates = [r for r in rows if r.id != survivor.id]
    dup_ids = [d.id for d in duplicates]

    # Point child rows at survivor (collision-safe)
    await _repoint_series_children(db, dup_ids, survivor.id, report)

    inherited_collection_id = survivor.collection_id

    for duplicate in duplicates:
        if inherited_collection_id is None and duplicate.collection_id is not None:
            inherited_collection_id = duplicate.collection_id

    await _merge_work_metadata(db, survivor, duplicates, protected)

    # Delete duplicates
    for d in duplicates:
        await db.delete(d)

    if survivor.collection_id is None and inherited_collection_id is not None:
        # Free the duplicate's unique collection/season slot before adopting it.
        # Keep both operations in the caller's transaction.
        await db.flush()
        survivor.collection_id = inherited_collection_id

    report.series_groups += 1
    report.series_removed += len(duplicates)
    report.notes.append(
        f"[series] kept={survivor.id} title={survivor.title_cn or survivor.title_en!r} "
        f"removed={len(duplicates)}"
    )


async def _repoint_movie_children(
    db: AsyncSession, dup_ids: list[str], survivor_id: str, report: DedupReport
) -> None:
    """Movie counterpart of :func:`_repoint_series_children` (no episodes)."""
    from app.services.decision_rekey import lock_work_choice_agents, rekey_agent_choices

    choice_agents = await lock_work_choice_agents(db, [("movie", [survivor_id, *dup_ids])])
    n = (await db.execute(
        update(FileResource)
        .where(FileResource.movie_id.in_(dup_ids))
        .values(movie_id=survivor_id)
    )).rowcount or 0
    report.file_resources_updated += n

    survivor_agents = {
        aw.agent_id
        for aw in (await db.execute(
            select(AgentWork).where(AgentWork.movie_id == survivor_id)
        )).scalars().all()
    }
    n = 0
    for aw in (await db.execute(
        select(AgentWork).where(AgentWork.movie_id.in_(dup_ids))
    )).scalars().all():
        if aw.agent_id in survivor_agents:
            await db.delete(aw)
        else:
            aw.movie_id = survivor_id
            survivor_agents.add(aw.agent_id)
            n += 1
    report.agent_works_updated += n

    # ChannelRawTitleMapping: uq (channel_id, search_title_key) is work-
    # independent, so re-pointing can never collide — bulk update is safe.
    n = (await db.execute(
        update(ChannelRawTitleMapping)
        .where(ChannelRawTitleMapping.movie_id.in_(dup_ids))
        .values(movie_id=survivor_id)
    )).rowcount or 0
    report.mappings_updated += n

    n = (await db.execute(update(PendingDecision).where(
        PendingDecision.movie_id.in_(dup_ids)
    ).values(movie_id=survivor_id))).rowcount or 0
    report.pending_decisions_updated += n

    # See _repoint_series_children: links/assignments must follow the survivor.
    links_n, assignments_n = await _repoint_enrichment_rows(
        db, src_movie_ids=dup_ids, dst_movie_id=survivor_id
    )
    report.work_links_updated += links_n
    report.file_assignments_updated += assignments_n
    await rekey_agent_choices(db, choice_agents)


async def _merge_movie_group(
    db: AsyncSession,
    rows: list[Movie],
    report: DedupReport,
    survivor: Movie | None = None,
    *,
    automatic: bool = False,
) -> None:
    if len(rows) < 2:
        return
    await lock_merge_works(db, rows)
    if automatic and not _group_still_matches(rows):
        report.notes.append("[candidate-changed] " + ",".join(sorted(row.id for row in rows)))
        return
    if survivor is None:
        survivor = select_survivor(rows)
    protected = require_protected_values(rows, survivor)
    duplicates = [r for r in rows if r.id != survivor.id]
    dup_ids = [d.id for d in duplicates]

    await _repoint_movie_children(db, dup_ids, survivor.id, report)

    for duplicate in duplicates:
        if survivor.collection_id is None and duplicate.collection_id is not None:
            survivor.collection_id = duplicate.collection_id

    await _merge_work_metadata(db, survivor, duplicates, protected)

    for d in duplicates:
        await db.delete(d)

    report.movie_groups += 1
    report.movies_removed += len(duplicates)
    report.notes.append(
        f"[movie] kept={survivor.id} title={survivor.title_cn or survivor.title_en!r} "
        f"removed={len(duplicates)}"
    )


async def merge_duplicate_series(db: AsyncSession, report: DedupReport | None = None) -> DedupReport:
    """Merge TVSeries rows that share any normalized title.

    Per-season works (作品单季化): clustering is isolated by
    ``season_number`` — different seasons of one IP share titles/aliases yet
    must never merge; legacy unsplit series-level rows only cluster among
    themselves. The year guard (remakes/reboots) still applies within a
    cluster.
    """
    report = report or DedupReport()
    all_series = list((await db.execute(select(TVSeries))).scalars().all())
    # Only cluster entities that carry at least one usable title
    keyed = [s for s in all_series if _title_keys(s)]
    for group in _cluster_by_shared_title(keyed, bucket_of=_series_cluster_bucket):
        if len(group) > 1:
            if _year_conflict([g.start_date for g in group]):
                report.notes.append(
                    "[series] skipped year-conflicting group: "
                    + ", ".join(
                        f"{g.id}({g.title_cn or g.title_en},{g.start_date})" for g in group
                    )
                )
                continue
            try:
                await _merge_series_group(db, group, report, automatic=True)
            except DedupConflictError as exc:
                report.notes.append(str(exc))
    return report


async def merge_duplicate_movies(db: AsyncSession, report: DedupReport | None = None) -> DedupReport:
    """Merge Movie rows that share any normalized title."""
    report = report or DedupReport()
    all_movies = list((await db.execute(select(Movie))).scalars().all())
    keyed = [m for m in all_movies if _title_keys(m)]
    for group in _cluster_by_shared_title(keyed):
        if len(group) > 1:
            if _year_conflict([g.release_date for g in group]):
                report.notes.append(
                    "[movie] skipped year-conflicting group: "
                    + ", ".join(
                        f"{g.id}({g.title_cn or g.title_en},{g.release_date})" for g in group
                    )
                )
                continue
            try:
                await _merge_movie_group(db, group, report, automatic=True)
            except DedupConflictError as exc:
                report.notes.append(str(exc))
    return report


async def merge_duplicate_metadata(db: AsyncSession) -> DedupReport:
    """Run TVSeries + Movie dedup in one transaction and return the report."""
    report = DedupReport()
    await merge_duplicate_series(db, report)
    await merge_duplicate_movies(db, report)
    await merge_cross_type_duplicates(db, report)
    return report


# ---------------------------------------------------------------------------
# Cross-table (Movie <-> TVSeries) duplicates
# ---------------------------------------------------------------------------


async def _episode_resource_count(db: AsyncSession, column, work_id: str) -> int:
    """Resources linked to a work that carry a per-season episode number -
    the strongest evidence that the work is actually a TV series."""
    n = (await db.execute(
        select(func.count()).select_from(FileResource).where(
            column == work_id,
            FileResource.episode.isnot(None),
            FileResource.is_batch.is_(False),
        )
    )).scalar_one()
    return int(n or 0)


async def merge_cross_type_duplicates(
    db: AsyncSession, report: DedupReport | None = None
) -> DedupReport:
    """Merge (Movie, TVSeries) pairs that describe the same external work.

    The per-table upserts converge duplicates within one table, but a wrong
    content_type verdict can still file the same entity (e.g.
    ``wikipedia:5139056``) once as a Movie and once as a TVSeries. Pairing
    rule: identical canonical external_id, or a shared normalized title key
    (titles + aliases, trad/simp-folded). Survivor rule: any episode-bearing
    resource (or Episode rows) on either side means the work is a series;
    otherwise the Movie survives. The loser's references are re-pointed and
    its titles/aliases merged before deletion.
    """
    report = report or DedupReport()
    movies = list((await db.execute(select(Movie))).scalars().all())
    series_all = list((await db.execute(select(TVSeries))).scalars().all())
    if not movies or not series_all:
        return report

    series_keys = {s.id: _title_keys(s) for s in series_all}
    removed_movies: set[str] = set()
    removed_series: set[str] = set()

    for movie in movies:
        if movie.id in removed_movies:
            continue
        m_keys = _title_keys(movie)
        for series in series_all:
            if series.id in removed_series:
                continue
            if not _same_cross_type_work(movie, series, m_keys, series_keys[series.id]):
                continue
            await lock_merge_works(db, [series, movie])
            if not _same_cross_type_work(movie, series, _title_keys(movie), _title_keys(series)):
                report.notes.append(f"[candidate-changed] {movie.id},{series.id}")
                continue

            # Survivor decision: episode evidence (or Episode rows) => series.
            series_ep_rows = int((await db.execute(
                select(func.count()).select_from(Episode).where(
                    Episode.series_id == series.id
                )
            )).scalar_one() or 0)
            movie_ep = await _episode_resource_count(db, FileResource.movie_id, movie.id)
            series_ep = await _episode_resource_count(db, FileResource.series_id, series.id)
            keep_series = bool(series_ep_rows or movie_ep or series_ep)
            target = series if keep_series else movie
            try:
                protected = require_protected_values([series, movie], target)
            except DedupConflictError as exc:
                report.notes.append(str(exc))
                continue

            from app.services.decision_rekey import lock_work_choice_agents, rekey_agent_choices

            choice_agents = await lock_work_choice_agents(db, [("series", [series.id]), ("movie", [movie.id])])
            if keep_series:
                n = (await db.execute(
                    update(FileResource)
                    .where(FileResource.movie_id == movie.id)
                    .values(series_id=series.id, movie_id=None)
                )).rowcount or 0
                report.file_resources_updated += n
                n = (await db.execute(
                    update(AgentWork)
                    .where(AgentWork.movie_id == movie.id)
                    .values(series_id=series.id, movie_id=None, content_type="tv")
                )).rowcount or 0
                report.agent_works_updated += n
                n = (await db.execute(
                    update(ChannelRawTitleMapping)
                    .where(ChannelRawTitleMapping.movie_id == movie.id)
                    .values(series_id=series.id, movie_id=None, content_type="tv")
                )).rowcount or 0
                report.mappings_updated += n
                n = (await db.execute(
                    update(PendingDecision)
                    .where(PendingDecision.movie_id == movie.id)
                    .values(series_id=series.id, movie_id=None)
                )).rowcount or 0
                report.pending_decisions_updated += n
                links_n, assignments_n = await _repoint_enrichment_rows(
                    db, src_movie_ids=[movie.id], dst_series_id=series.id
                )
                report.work_links_updated += links_n
                report.file_assignments_updated += assignments_n


                # P3: union identity bags; otherwise the movie's bag rows
                # would dangle at a deleted work id.
                await rekey_agent_choices(db, choice_agents)
                await _merge_work_metadata(db, series, [movie], protected)

                await db.delete(movie)
                removed_movies.add(movie.id)
                report.cross_type_merges += 1
                report.notes.append(
                    f"[cross-type] movie {movie.id} merged into series {series.id} "
                    f"({movie.title_cn or movie.title_en!r}; episode evidence)"
                )
                break  # movie gone - next movie
            else:
                n = (await db.execute(
                    update(FileResource)
                    .where(FileResource.series_id == series.id)
                    .values(movie_id=movie.id, series_id=None)
                )).rowcount or 0
                report.file_resources_updated += n
                n = (await db.execute(
                    update(AgentWork)
                    .where(AgentWork.series_id == series.id)
                    .values(movie_id=movie.id, series_id=None, content_type="movie")
                )).rowcount or 0
                report.agent_works_updated += n
                n = (await db.execute(
                    update(ChannelRawTitleMapping)
                    .where(ChannelRawTitleMapping.series_id == series.id)
                    .values(movie_id=movie.id, series_id=None, content_type="movie")
                )).rowcount or 0
                report.mappings_updated += n
                n = (await db.execute(
                    update(PendingDecision)
                    .where(PendingDecision.series_id == series.id)
                    .values(movie_id=movie.id, series_id=None)
                )).rowcount or 0
                report.pending_decisions_updated += n
                links_n, assignments_n = await _repoint_enrichment_rows(
                    db, src_series_ids=[series.id], dst_movie_id=movie.id
                )
                report.work_links_updated += links_n
                report.file_assignments_updated += assignments_n


                # P3: union identity bags (symmetric to the branch above).
                await rekey_agent_choices(db, choice_agents)
                await _merge_work_metadata(db, movie, [series], protected)

                await db.delete(series)
                removed_series.add(series.id)
                report.cross_type_merges += 1
                report.notes.append(
                    f"[cross-type] series {series.id} merged into movie {movie.id} "
                    f"({series.title_cn or series.title_en!r}; no episode evidence)"
                )
                # series gone - keep scanning remaining series for this movie
    return report
