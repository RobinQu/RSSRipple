"""Exact batch coverage from authoritative assignments or explicit title bounds.

No lazy loading or network calls. Callers must load work_links and
file_assignments; an unloaded relation is unknown, never an empty manifest.
Dispatch uses this descriptor; persistent decision keys remain a separate migration.
"""
from sqlalchemy import inspect

from app.services.filter_engine import loaded_relation
from app.services.metadata_episode_reconcile import is_unsplit_legacy_series


def _loaded(obj, name):
    state = inspect(obj, raiseerr=False)
    if state is not None and name in state.unloaded:
        return None
    return loaded_relation(obj, name)


def merge_intervals(intervals):
    """Canonical union without expanding episode numbers or filling gaps."""
    merged = []
    for lo, hi in sorted(intervals):
        if merged and lo <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
        else:
            merged.append((lo, hi))
    return tuple(merged)


def _interval(start, end):
    if start is None:
        start = end
    if end is None:
        end = start
    if not all(type(value) is int and value >= 0 for value in (start, end)) or start > end:
        return None
    return start, end


def batch_coverage(resource):
    """Return sorted work/season/interval tuples, or None for unknown coverage.

    File assignments are authoritative when present: bad/incomplete bindings
    cannot fall back to a broader title interval. Each linked work must be
    represented. This does not independently certify torrent completeness;
    that remains the upstream association/manifest validation contract.
    """
    if not resource.is_batch:
        return None
    links = _loaded(resource, 'work_links')
    assignments = _loaded(resource, 'file_assignments')
    if links is None or assignments is None:
        return None
    targets = set()
    target_seasons = {}
    for obj in [resource, *links]:
        series_id, movie_id = obj.series_id, obj.movie_id
        if series_id and movie_id:
            return None
        if series_id:
            work = _loaded(obj, 'series')
            season = getattr(work, 'season_number', None)
            if work is None or work.id != series_id or type(season) is not int or season < 0:
                return None
            if is_unsplit_legacy_series(work):
                return None
            if series_id in target_seasons and target_seasons[series_id] != season:
                return None
            target_seasons[series_id] = season
            targets.add(('series', series_id))
        if movie_id:
            targets.add(('movie', movie_id))
    if not targets:
        return None
    if assignments:
        groups = {}
        seen = set()
        for row in assignments:
            if row.series_id and not row.movie_id:
                target = ('series', row.series_id)
                interval = _interval(row.episode_start, row.episode_end)
                if target not in targets or interval is None or type(row.season) is not int or row.season < 0:
                    return None
                if row.season != target_seasons[row.series_id]:
                    return None
                seen.add(target)
                groups.setdefault((*target, row.season), []).append(interval)
            elif row.movie_id and not row.series_id:
                target = ('movie', row.movie_id)
                if target not in targets:
                    return None
                seen.add(target)
                groups.setdefault((*target, None), [])
            else:
                return None
        if seen != targets:
            return None
        # A season-work ID may never contain multiple seasons. Even a
        # legacy unsplit row is held for migration rather than guessed.
        seasons = {}
        for kind, identity, season in groups:
            if kind == 'series':
                if identity in seasons and seasons[identity] != season:
                    return None
                seasons[identity] = season
        return tuple((*key, merge_intervals(values)) for key, values in sorted(groups.items()))
    if len(targets) != 1:
        return None
    kind, identity = next(iter(targets))
    if kind == 'movie':
        return ((kind, identity, None, ()),)
    if resource.batch_scope != 'season' or type(resource.season) is not int or resource.season < 0:
        return None
    # Both title bounds must be explicit. A lone start is not proof of a
    # one-episode pack, unlike a per-file assignment with one endpoint.
    if resource.episode_start is None or resource.episode_end is None:
        return None
    if resource.season != target_seasons[identity]:
        return None
    interval = _interval(resource.episode_start, resource.episode_end)
    return ((kind, identity, resource.season, (interval,)),) if interval is not None else None


async def load_batch_coverage(db, resources):
    """Load coverage in bounded groups before synchronous policy evaluation."""
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from app.models.file_resource import FileResource
    from app.models.movie import Movie
    from app.models.resource_work_link import ResourceWorkLink
    from app.models.series import TVSeries

    ids = sorted({r.id for r in resources if r.is_batch and r.id})
    for offset in range(0, len(ids), 100):
        await db.execute(
            select(FileResource).where(FileResource.id.in_(ids[offset:offset + 100])).options(
                selectinload(FileResource.series).selectinload(TVSeries.collection),
                selectinload(FileResource.movie).selectinload(Movie.collection),
                selectinload(FileResource.audio_work),
                selectinload(FileResource.collection),
                selectinload(FileResource.work_links).selectinload(ResourceWorkLink.series),
                selectinload(FileResource.work_links).selectinload(ResourceWorkLink.movie),
                selectinload(FileResource.file_assignments),
            ).execution_options(populate_existing=True)
        )
