"""One-time deduplication of TVSeries / Movie rows created before the
canonical-external-id upsert was in place.

Historically, Exa Agent Search returned the same TMDB id in inconsistent shapes
across successive fetches (e.g. ``TMDB:82684``, ``TMDB 82684``,
``TMDB TV 82684 / season 4``). The old
``create_or_update_series_from_external`` looked up rows by the raw
``external_id`` string, so every new shape spawned a new TVSeries entity for
the same real work. This module merges those duplicate rows into a canonical
survivor and re-points all references (FileResource / ChannelRawTitleMapping /
AgentWork / PendingDecision / Episode) at the survivor.

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

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_work import AgentWork
from app.models.channel_raw_title_mapping import ChannelRawTitleMapping
from app.models.episode import Episode
from app.models.file_resource import FileResource
from app.models.movie import Movie
from app.models.pending_decision import PendingDecision
from app.models.series import TVSeries
from app.services.metadata_service import canonicalize_external_id
from app.services.text_normalizer import normalize_title

logger = logging.getLogger(__name__)


@dataclass
class DedupReport:
    series_groups: int = 0
    series_removed: int = 0
    movie_groups: int = 0
    movies_removed: int = 0
    file_resources_updated: int = 0
    agent_works_updated: int = 0
    mappings_updated: int = 0
    pending_decisions_updated: int = 0
    episodes_updated: int = 0
    notes: list[str] = field(default_factory=list)


def _title_keys(entity: TVSeries | Movie) -> set[str]:
    """Normalized title keys used to link related entities together.

    An entity may be reachable via any of ``title_cn`` / ``title_en`` /
    ``original_title``; two entities are considered the same work if they share
    any of these keys (union-find style). This catches cases like
    ``"第四季"`` vs ``"第 4 季"`` where ``title_cn`` differs but ``title_en``
    is identical.
    """
    keys: set[str] = set()
    for raw in (entity.title_cn, entity.title_en, entity.original_title):
        n = normalize_title(raw)
        if n:
            keys.add(n)
    return keys


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


def _cluster_by_shared_title(entities: list) -> list[list]:
    """Group ``entities`` so that any two sharing a normalized title end up in
    the same cluster."""
    uf = _UnionFind()
    keyed: dict[str, list[str]] = {}
    id_to_entity = {e.id: e for e in entities}
    for e in entities:
        uf.add(e.id)
        for k in _title_keys(e):
            keyed.setdefault(k, []).append(e.id)
    for ids in keyed.values():
        first = ids[0]
        for other in ids[1:]:
            uf.union(first, other)
    return [[id_to_entity[i] for i in ids] for ids in uf.groups().values()]


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


async def _merge_series_group(
    db: AsyncSession, rows: list[TVSeries], report: DedupReport
) -> None:
    if len(rows) < 2:
        return
    rows.sort(key=lambda r: (r.created_at, r.id))
    survivor, *duplicates = rows
    dup_ids = [d.id for d in duplicates]

    # Point child rows at survivor
    n = (await db.execute(
        update(FileResource)
        .where(FileResource.series_id.in_(dup_ids))
        .values(series_id=survivor.id)
    )).rowcount or 0
    report.file_resources_updated += n
    n = (await db.execute(
        update(AgentWork)
        .where(AgentWork.series_id.in_(dup_ids))
        .values(series_id=survivor.id)
    )).rowcount or 0
    report.agent_works_updated += n
    n = (await db.execute(
        update(ChannelRawTitleMapping)
        .where(ChannelRawTitleMapping.series_id.in_(dup_ids))
        .values(series_id=survivor.id)
    )).rowcount or 0
    report.mappings_updated += n
    n = (await db.execute(
        update(PendingDecision)
        .where(PendingDecision.series_id.in_(dup_ids))
        .values(series_id=survivor.id)
    )).rowcount or 0
    report.pending_decisions_updated += n
    n = (await db.execute(
        update(Episode)
        .where(Episode.series_id.in_(dup_ids))
        .values(series_id=survivor.id)
    )).rowcount or 0
    report.episodes_updated += n

    # Enrich survivor from duplicates
    survivor.aliases = _merge_aliases(rows)
    canonical_ext = _pick_canonical_external_id(rows)
    if canonical_ext:
        survivor.external_id = canonical_ext
    # Prefer any non-None poster/description/etc from duplicates when survivor lacks it
    for d in duplicates:
        if not survivor.title_cn and d.title_cn:
            survivor.title_cn = d.title_cn
        if not survivor.title_en and d.title_en:
            survivor.title_en = d.title_en
        if not survivor.original_title and d.original_title:
            survivor.original_title = d.original_title
        if not (survivor.poster_url or "").startswith("/posters/") and d.poster_url:
            survivor.poster_url = d.poster_url
        if not survivor.description and d.description:
            survivor.description = d.description
        if survivor.rating is None and d.rating is not None:
            survivor.rating = d.rating
        if not survivor.genre and d.genre:
            survivor.genre = d.genre
        if survivor.number_of_episodes is None and d.number_of_episodes is not None:
            survivor.number_of_episodes = d.number_of_episodes
        if survivor.number_of_seasons is None and d.number_of_seasons is not None:
            survivor.number_of_seasons = d.number_of_seasons

    # Delete duplicates
    for d in duplicates:
        await db.delete(d)

    report.series_groups += 1
    report.series_removed += len(duplicates)
    report.notes.append(
        f"[series] kept={survivor.id} title={survivor.title_cn or survivor.title_en!r} "
        f"removed={len(duplicates)}"
    )


async def _merge_movie_group(
    db: AsyncSession, rows: list[Movie], report: DedupReport
) -> None:
    if len(rows) < 2:
        return
    rows.sort(key=lambda r: (r.created_at, r.id))
    survivor, *duplicates = rows
    dup_ids = [d.id for d in duplicates]

    n = (await db.execute(
        update(FileResource)
        .where(FileResource.movie_id.in_(dup_ids))
        .values(movie_id=survivor.id)
    )).rowcount or 0
    report.file_resources_updated += n
    n = (await db.execute(
        update(AgentWork)
        .where(AgentWork.movie_id.in_(dup_ids))
        .values(movie_id=survivor.id)
    )).rowcount or 0
    report.agent_works_updated += n
    n = (await db.execute(
        update(ChannelRawTitleMapping)
        .where(ChannelRawTitleMapping.movie_id.in_(dup_ids))
        .values(movie_id=survivor.id)
    )).rowcount or 0
    report.mappings_updated += n
    n = (await db.execute(
        update(PendingDecision)
        .where(PendingDecision.movie_id.in_(dup_ids))
        .values(movie_id=survivor.id)
    )).rowcount or 0
    report.pending_decisions_updated += n

    survivor.aliases = _merge_aliases(rows)
    canonical_ext = _pick_canonical_external_id(rows)
    if canonical_ext:
        survivor.external_id = canonical_ext
    for d in duplicates:
        if not survivor.title_cn and d.title_cn:
            survivor.title_cn = d.title_cn
        if not survivor.title_en and d.title_en:
            survivor.title_en = d.title_en
        if not survivor.original_title and d.original_title:
            survivor.original_title = d.original_title
        if not (survivor.poster_url or "").startswith("/posters/") and d.poster_url:
            survivor.poster_url = d.poster_url
        if not survivor.description and d.description:
            survivor.description = d.description
        if survivor.rating is None and d.rating is not None:
            survivor.rating = d.rating
        if not survivor.genre and d.genre:
            survivor.genre = d.genre
        if survivor.runtime is None and d.runtime is not None:
            survivor.runtime = d.runtime

    for d in duplicates:
        await db.delete(d)

    report.movie_groups += 1
    report.movies_removed += len(duplicates)
    report.notes.append(
        f"[movie] kept={survivor.id} title={survivor.title_cn or survivor.title_en!r} "
        f"removed={len(duplicates)}"
    )


async def merge_duplicate_series(db: AsyncSession, report: DedupReport | None = None) -> DedupReport:
    """Merge TVSeries rows that share any normalized title."""
    report = report or DedupReport()
    all_series = list((await db.execute(select(TVSeries))).scalars().all())
    # Only cluster entities that carry at least one usable title
    keyed = [s for s in all_series if _title_keys(s)]
    for group in _cluster_by_shared_title(keyed):
        if len(group) > 1:
            await _merge_series_group(db, group, report)
    return report


async def merge_duplicate_movies(db: AsyncSession, report: DedupReport | None = None) -> DedupReport:
    """Merge Movie rows that share any normalized title."""
    report = report or DedupReport()
    all_movies = list((await db.execute(select(Movie))).scalars().all())
    keyed = [m for m in all_movies if _title_keys(m)]
    for group in _cluster_by_shared_title(keyed):
        if len(group) > 1:
            await _merge_movie_group(db, group, report)
    return report


async def merge_duplicate_metadata(db: AsyncSession) -> DedupReport:
    """Run TVSeries + Movie dedup in one transaction and return the report."""
    report = DedupReport()
    await merge_duplicate_series(db, report)
    await merge_duplicate_movies(db, report)
    return report
