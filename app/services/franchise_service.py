"""Franchise pack member linking (torrent content detection, final layer).

When ``maybe_inspect_torrent`` classifies a torrent as ``batch_scope ==
"franchise"`` (two or more distinct work clusters, e.g. "作品X TV" +
"作品X 剧场版"), this service resolves each cluster title in
``TorrentReport.work_titles`` to a real work row and groups them under a
``WorkCollection``:

1. Each cluster title is matched via the channel-configured
   ``UnifiedMetadataAgent.process_title_only`` (the same title-only matching
   used by manual search / eval — no new matching logic here). A hit
   (``found`` + ``matched_entity`` carrying a title) is upserted through the
   existing ``create_or_update_series_from_external`` /
   ``create_or_update_movie_from_external`` path, so identity-bag /
   canonical-id / title convergence and idempotency come for free.
2. With at least one resolved member, a ``WorkCollection`` is fetched or
   created (get-or-create key: ``external_source="franchise_pack"`` +
   normalized title equality — these collections carry no external id, so
   ``external_id`` stays NULL and the title is the identity). Member works
   are attached via ``collection_id``; a work already belonging to another
   collection is never stolen (one work, at most one collection).
3. The resource itself links to the collection (``resource.collection_id``)
   and the FK-exclusivity invariant is enforced — ``series_id`` /
   ``movie_id`` / ``audio_work_id`` are all cleared.

When every member fails to resolve, the resource keeps only the batch
verdict (``is_batch`` / ``batch_scope``) that ``maybe_inspect_torrent``
already wrote; no collection is created.

The function does NOT commit: like ``maybe_inspect_torrent`` it runs inside
``fetch_service._process_resource_metadata``'s task session, whose own
commit persists the changes. Re-running it is idempotent (upserts +
get-or-create converge on the same rows).
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from sqlalchemy import select

from app.models.work_collection import WorkCollection
from app.services.resource_parser import extract_compilation_work_title
from app.services.text_normalizer import normalize_title

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.channel import Channel
    from app.models.file_resource import FileResource
    from app.models.movie import Movie
    from app.models.series import TVSeries
    from app.services.torrent_inspect import TorrentReport

logger = logging.getLogger(__name__)

# ``external_source`` for collections created from franchise torrent packs.
# ``external_id`` stays NULL (no upstream identity) — get-or-create keys off
# the normalized title instead; the (source, external_id) unique constraint
# allows multiple NULLs on both SQLite/Turso and PostgreSQL.
FRANCHISE_PACK_SOURCE = "franchise_pack"

_TITLE_KEYS = ("title_cn", "title_en", "original_title")

# Pack-title cleanup, mirroring the cluster-title normalization in
# torrent_inspect: bracketed release tags are decoration, not the work name.
_BRACKET_BLOCK_RE = re.compile(r"[\[【\(（][^\]】\)）]*[\]】\)）]")


def _pack_title(resource: FileResource) -> str:
    """Display title for the pack's WorkCollection.

    Prefers the resource's cleaned main title (``search_title``/``title_cn``);
    falls back to the compilation-style extraction of the raw title
    (``extract_compilation_work_title`` handles "[整理搬运] 作品：TV+剧场版…"
    shapes) and finally the raw title itself.
    """
    base = (resource.search_title or resource.title_cn or "").strip()
    if not base:
        base = extract_compilation_work_title(resource.title_raw) or resource.title_raw or ""
    t = _BRACKET_BLOCK_RE.sub(" ", base)
    t = re.sub(r"\s+", " ", t).strip(" -_.·　")
    return (t or base or "franchise pack")[:512]


async def _get_or_create_franchise_collection(
    db: AsyncSession, pack_title: str
) -> WorkCollection:
    """Get-or-create a franchise-pack WorkCollection by normalized title."""
    norm = normalize_title(pack_title)
    rows = (
        await db.execute(
            select(WorkCollection).where(
                WorkCollection.external_source == FRANCHISE_PACK_SOURCE
            )
        )
    ).scalars().all()
    for coll in rows:
        if normalize_title(coll.title_cn) == norm:
            return coll
        if coll.title_en and normalize_title(coll.title_en) == norm:
            return coll
    collection = WorkCollection(
        title_cn=pack_title,
        external_id=None,
        external_source=FRANCHISE_PACK_SOURCE,
    )
    db.add(collection)
    await db.flush()
    return collection


async def _resolve_member(
    db: AsyncSession, agent, source: str, title: str
) -> TVSeries | Movie | None:
    """Resolve one cluster title to a work row, or None on any failure."""
    try:
        meta = await agent.process_title_only(title, source)
    except Exception as e:
        logger.warning("[franchise] member match failed for %r: %s", title[:80], e)
        return None
    if not meta or not meta.found or not meta.matched_entity:
        logger.info("[franchise] member %r not found (source=%s)", title[:80], source)
        return None
    entity = dict(meta.matched_entity)
    if not any(entity.get(k) for k in _TITLE_KEYS):
        logger.warning("[franchise] member %r matched entity has no title; skipped", title[:80])
        return None

    from app.services.metadata_service import (
        create_or_update_movie_from_external,
        create_or_update_series_from_external,
    )

    try:
        if meta.content_type == "movie":
            return await create_or_update_movie_from_external(db, entity)
        if meta.content_type == "tv":
            return await create_or_update_series_from_external(db, entity)
    except Exception as e:
        logger.warning("[franchise] member upsert failed for %r: %s", title[:80], e)
        return None
    logger.info(
        "[franchise] member %r resolved to non-tv/movie content_type=%r; skipped",
        title[:80], meta.content_type,
    )
    return None


async def link_franchise_pack(
    db: AsyncSession,
    resource: FileResource,
    report: TorrentReport,
    channel: Channel,
) -> None:
    """Link a franchise-pack resource to a WorkCollection of member works.

    See the module docstring for the full contract. No-ops with a warning
    when no member resolves — the caller's batch classification
    (``is_batch`` / ``batch_scope="franchise"``) is left untouched either
    way.
    """
    if not report.work_titles:
        logger.warning("[franchise] resource %s: report has no work_titles", resource.id)
        return

    # Local import at call time so tests can patch
    # ``app.services.metadata_agent.get_agent`` (same pattern as fetch_service).
    from app.services.metadata_agent import get_agent
    from app.services.metadata_sources import resolve_metadata_source

    source = resolve_metadata_source(getattr(channel, "metadata_source", None))
    agent = get_agent()

    works: list[TVSeries | Movie] = []
    seen: set[tuple[str, str]] = set()
    for title in report.work_titles:
        work = await _resolve_member(db, agent, source, title)
        if work is None:
            continue
        key = (type(work).__name__, work.id)
        if key not in seen:
            seen.add(key)
            works.append(work)

    if not works:
        logger.warning(
            "[franchise] resource %s: all %d members failed to resolve; "
            "keeping batch verdict only",
            resource.id, len(report.work_titles),
        )
        return

    collection = await _get_or_create_franchise_collection(db, _pack_title(resource))
    for work in works:
        if work.collection_id and work.collection_id != collection.id:
            logger.warning(
                "[franchise] work %s already belongs to collection %s; "
                "not re-attaching to %s",
                work.id, work.collection_id, collection.id,
            )
            continue
        work.collection_id = collection.id

    resource.collection_id = collection.id
    # FK-exclusivity invariant: a collection-linked resource carries no
    # per-work FK.
    resource.series_id = None
    resource.movie_id = None
    resource.audio_work_id = None
    logger.info(
        "[franchise] resource %s linked to collection %r (%d/%d members resolved)",
        resource.id, collection.title_cn, len(works), len(report.work_titles),
    )
