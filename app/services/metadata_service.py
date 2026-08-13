"""Metadata matching service for TVSeries and Movie entities.

Matching flow for a FileResource (per AGENTS.md "Metadata 匹配流程"):
1. Already linked (movie_id / series_id set) → return.
2. ChannelRawTitleMapping exact match by (channel_id, raw_title).
3. Local DB match: exact (title_cn/title_en) then fuzzy (ratio >= 70; auto-link at >=85).
4. Unified MetadataAgent (ReAct agent) — uses one selected metadata source
   (channel.metadata_agent_enabled == True).
5. Link FileResource.movie_id or FileResource.series_id.

Poster caching: poster URLs returned by LLM are downloaded to POSTER_CACHE_DIR
using a sha256-based filename, and the DB pointer is updated to /posters/<file>.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.audio_work import AudioWork
from app.models.channel_raw_title_mapping import ChannelRawTitleMapping
from app.models.episode import Episode
from app.models.movie import Movie
from app.models.series import TVSeries
from app.services import fts as fts_service
from app.services.anime_signals import apply_is_anime
from app.services.external_ids import add_external_id, find_work_by_external_id
from app.services.genre_registry import normalize_genres
from app.services.metadata_source_registry import canonicalize_external_id  # noqa: F401
from app.services.resource_parser import strip_season_from_title
from app.services.text_normalizer import normalize_title, similarity_score
from app.utils.time import utcnow

logger = logging.getLogger(__name__)

FUZZY_THRESHOLD = 70
AUTO_LINK_THRESHOLD = 85


def _record_unmatched_attempt(resource: Any, failure_type: str) -> None:
    """Stamp retry-state columns when a resource ends a pass still unmatched.

    Mirrors ``metadata_agent._record_metadata_attempt`` for the non-agent
    path so the fetch-time backfill can apply the same backoff/TTL policy
    regardless of which matcher ran. Only called on unmatched exits —
    matched exits set ``metadata_matched_at`` and are excluded from backfill
    by the ``series_id/movie_id IS NULL`` filter.
    """
    resource.metadata_attempts = int(getattr(resource, "metadata_attempts", 0) or 0) + 1
    resource.last_metadata_attempt_at = utcnow()
    resource.metadata_failure_type = failure_type


async def apply_channel_default_is_anime(
    db: AsyncSession, channel: Any, resource: Any
) -> None:
    """Channel-level "默认标记为 Anime": when the channel has
    ``default_is_anime`` enabled, any work linked from one of its resources
    is marked ``is_anime=True`` (sticky — an existing True is untouched, and
    a confirmed False is upgraded, matching ``apply_is_anime`` semantics).
    Call after every successful resource→work link."""
    if not getattr(channel, "default_is_anime", False):
        return
    work = None
    if getattr(resource, "series_id", None):
        work = await db.get(TVSeries, resource.series_id)
    elif getattr(resource, "movie_id", None):
        work = await db.get(Movie, resource.movie_id)
    if work is not None and work.is_anime is not True:
        work.is_anime = True


async def maybe_verify_is_anime_via_bangumi(
    db: AsyncSession, channel: Any, resource: Any
) -> None:
    """Layer-1 is_anime detection for channels WITHOUT the default flag.

    When the linked work's ``is_anime`` is still undetermined (None) and a
    Bangumi token is configured, search Bangumi by the work's titles and
    apply :func:`anime_signals.bangumi_verdict` — a type-2 (anime) hit sets
    True, a type-6 (三次元) hit sets False. Runs after every successful
    resource→work link; already-determined works are skipped.
    """
    from app.services.anime_signals import bangumi_verdict
    from app.services.bangumi_client import bangumi_configured, search_subjects

    if getattr(channel, "default_is_anime", False):
        return  # the default flag already handles this channel's works
    if not bangumi_configured():
        return
    work = None
    year = None
    if getattr(resource, "series_id", None):
        work = await db.get(TVSeries, resource.series_id)
        year = work.start_date.year if work and work.start_date else None
    elif getattr(resource, "movie_id", None):
        work = await db.get(Movie, resource.movie_id)
        year = work.release_date.year if work and work.release_date else None
    if work is None or work.is_anime is not None:
        return

    titles = [work.title_cn, work.original_title, work.title_en, *(work.aliases or [])]
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            for query in (work.title_cn, work.original_title, work.title_en):
                if not query:
                    continue
                subjects = await search_subjects(client, query)
                verdict, subj = bangumi_verdict(titles, year, subjects)
                if verdict is not None:
                    work.is_anime = verdict
                    logger.info(
                        "[metadata] is_anime=%s via bangumi#%s for %r",
                        verdict, subj.get("id"),
                        (work.title_cn or work.title_en or "")[:60],
                    )
                    return
                await asyncio.sleep(0.3)  # be polite between candidate queries
    except Exception as e:
        logger.warning(
            "[metadata] bangumi is_anime verification failed for %r: %s",
            (work.title_cn or work.title_en or "")[:60], e,
        )


async def classify_is_anime_post_link(
    db: AsyncSession, channel: Any, resource: Any
) -> None:
    """Post-link is_anime classification: the channel default flag first,
    then the Bangumi layer-1 verification for still-undetermined works."""
    await apply_channel_default_is_anime(db, channel, resource)
    await maybe_verify_is_anime_via_bangumi(db, channel, resource)


# ---------------------------------------------------------------------------
# external_id canonicalization — moved to ``metadata_source_registry`` (the
# single authority for external identity sites, Phase P1). Imported at the
# top of this module and re-exported here for the many existing callers
# (upsert paths, dedup, scripts).
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Title extraction helpers
# ---------------------------------------------------------------------------

_EPISODE_TAIL_RE = re.compile(r"\s*-\s*\d+\b.*$")
_SEASON_EPISODE_RE = re.compile(r"\s+S\d+E\d+\b.*$", re.IGNORECASE)
# Leading subtitle-group bracket pair: [Group] or 【Group】. Only ONE pair is
# stripped so a bracketed work name in a multi-bracket title (e.g.
# "[SweetSub][小書痴...][S04]") is preserved for the LLM agent / candidate
# queries to handle - dropping all brackets would delete it.
_LEADING_BRACKET_PAIR_RE = re.compile(r"^[\[【][^\]】]*[\]】]\s*")
# Loose S0N season token and a trailing standalone episode number, e.g.
# "Show S04 13" -> "Show".
_STRAY_SEASON_TOKEN_RE = re.compile(r"\s+S\d{1,2}\b", flags=re.IGNORECASE)
_TRAILING_EPISODE_NUM_RE = re.compile(r"\s+\d{1,3}\s*$")
# A single trailing bracket pair (quality/codec tag like "[1080p]"). Only one
# pass so an all-bracket title ("[Work][meta][meta]") keeps its content.
_TRAILING_BRACKET_PAIR_RE = re.compile(r"\s*[\[【][^\]】]*[\]】]\s*$")


def _finalize_search_title(s: str) -> str:
    """Final cleanup for an extracted search title.

    Strips a trailing season suffix (第三季 / S04 / Season 4 / 3期) so the work
    title matches its base-form entry in the local DB / title index, then trims
    decorative separators. ``strip_season_from_title`` already falls back to
    the input when stripping would empty it.
    """
    return strip_season_from_title(s).strip(" -:：·|/")


def extract_search_title(resource: Any) -> str:
    """Extract a base searchable title from a FileResource (sync, no LLM).

    Priority:
    1. ``title_cn`` or ``title_en`` (already parsed by field_mapping), with a
       trailing season suffix stripped so it matches the base work title.
    2. ``title_raw`` cleaned: drop the leading [subtitle group] bracket, take
       the segment before a `` / `` alt-title separator, strip the episode
       tail / SxxExx / stray season token / trailing episode number, then
       strip a trailing season suffix.
    3. Simple regex cleanup of ``title_raw`` as a last resort.

    Conservative by design: many fansub titles bury the work name inside
    bracket pairs (``[Group][Work][S04][13]``) where no regex can reliably
    separate it from release metadata. Those are resolved by the LLM agent
    (which sets a clean title on success) and the Wikipedia candidate-query
    builder; this function only needs to be "good enough" for local FTS
    matching and as a fallback when the agent is disabled or fails.
    """
    title = resource.title_cn or resource.title_en
    if title and title.strip():
        return _finalize_search_title(title.strip())

    raw = getattr(resource, "title_raw", None) or ""
    if not raw.strip():
        return raw

    cleaned = _LEADING_BRACKET_PAIR_RE.sub("", raw)
    # Multi-language alt titles are separated by " / "; keep only the first
    # segment as the primary work name. Per-language variants for Wikipedia
    # search are derived separately by the candidate-query builder.
    cleaned = re.split(r"\s*/\s*", cleaned, maxsplit=1)[0]
    cleaned = _EPISODE_TAIL_RE.sub("", cleaned)
    cleaned = _SEASON_EPISODE_RE.sub("", cleaned)
    cleaned = _STRAY_SEASON_TOKEN_RE.sub("", cleaned)
    cleaned = _TRAILING_EPISODE_NUM_RE.sub("", cleaned)
    cleaned = _TRAILING_BRACKET_PAIR_RE.sub("", cleaned)
    cleaned = _finalize_search_title(cleaned)
    return cleaned or raw.strip()


# ---------------------------------------------------------------------------
# Poster caching
# ---------------------------------------------------------------------------

def _sniff_image_ext(content: bytes) -> str | None:
    """Actual image format from magic bytes (``svg`` for XML/SVG markup).

    The cached file's extension must come from the content, not the URL —
    image hosts (Wikimedia among them) serve SVG logos at extension-less or
    misleading paths, and SVG bytes stored as ``.jpg`` render as a broken
    image in browsers (StaticFiles sends ``image/jpeg`` by suffix).
    Returns ``None`` for unrecognized content.
    """
    if content[:3] == b"\xff\xd8\xff":
        return "jpg"
    if content[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "webp"
    if content[:4] == b"GIF8":
        return "gif"
    head = content[:512].lstrip()
    if head[:5].lower() == b"<?xml" or head[:4].lower() == b"<svg":
        return "svg"
    return None


async def download_and_cache_poster(remote_url: str | None) -> str | None:
    """Download a poster image to the local cache directory.

    Filename is ``{sha256(url)[:16]}.{ext}`` where ``ext`` is sniffed from the
    downloaded bytes (:func:`_sniff_image_ext`), never from the URL. Returns
    the local URL path ``/posters/<filename>`` on success, or None on failure
    (including unrecognized/non-image content, which is not cached).
    Skips URLs that are already local (``/posters/...``).
    """
    if not remote_url:
        return None
    if remote_url.startswith("/posters/"):
        return remote_url
    if not (remote_url.startswith("http://") or remote_url.startswith("https://")):
        return None
    if not settings.poster_cache_dir:
        return None

    cache_dir = Path(settings.poster_cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)

    digest = hashlib.sha256(remote_url.encode("utf-8")).hexdigest()[:16]
    # Already cached under any known extension.
    for ext in ("jpg", "jpeg", "png", "webp", "gif", "svg"):
        if (cache_dir / f"{digest}.{ext}").exists():
            return f"/posters/{digest}.{ext}"

    def _download() -> bytes | None:
        try:
            # Wikimedia upload servers (upload.wikimedia.org) 403 requests
            # without a descriptive User-Agent; harmless for TMDB image hosts.
            ua = (
                f"{settings.app_name}/0.1.0 "
                f"(https://github.com/RobinQu/RSSRipple) metadata-agent"
            )
            with httpx.Client(
                timeout=30, follow_redirects=True, headers={"User-Agent": ua}
            ) as client:
                resp = client.get(remote_url)
                resp.raise_for_status()
                return resp.content
        except Exception as e:
            logger.warning("[poster] download failed %s: %s", remote_url[:80], e)
            return None

    content = await asyncio.to_thread(_download)
    if not content:
        return None
    ext = _sniff_image_ext(content)
    if ext is None:
        logger.warning("[poster] unrecognized image content %s", remote_url[:80])
        return None
    filename = f"{digest}.{ext}"
    local_path = cache_dir / filename
    try:
        local_path.write_bytes(content)
        return f"/posters/{filename}"
    except Exception as e:
        logger.warning("[poster] write failed %s: %s", filename, e)
        return None


# ---------------------------------------------------------------------------
# Local DB matching
# ---------------------------------------------------------------------------

async def match_series_by_title(db: AsyncSession, title: str) -> tuple[TVSeries | None, int]:
    """Find best matching TVSeries in local DB. Returns (entity, score 0-100).

    Uses FTS5 trigram search for candidate retrieval (no full-table scan),
    then computes bigram Dice similarity for precise ranking.
    """
    if not title:
        return None, 0
    norm = normalize_title(title)
    if not norm:
        return None, 0

    # 1. Exact match on original title_cn/title_en (fast SQL index lookup)
    result = await db.execute(
        select(TVSeries).where(
            or_(
                TVSeries.title_cn == title,
                TVSeries.title_en == title,
            )
        )
    )
    series = result.scalars().first()
    if series:
        return series, 100

    # 2. FTS5 candidate retrieval + similarity scoring
    candidate_ids = await fts_service.search_series_fts(db, title, limit=30)
    if candidate_ids:
        result = await db.execute(select(TVSeries).where(TVSeries.id.in_(candidate_ids)))
        candidates = result.scalars().all()
    else:
        # FTS index may be empty/out of sync — fall back to full-table scan
        result = await db.execute(select(TVSeries))
        candidates = result.scalars().all()

    best: TVSeries | None = None
    best_score = 0
    for s in candidates:
        titles = [s.title_cn, s.title_en, *(s.aliases or [])]
        score = max((similarity_score(norm, t) for t in titles if t), default=0)
        if score > best_score:
            best_score = score
            best = s

    if best_score >= FUZZY_THRESHOLD:
        return best, best_score
    return None, 0


async def match_movie_by_title(db: AsyncSession, title: str) -> tuple[Movie | None, int]:
    """Find best matching Movie in local DB. Returns (entity, score 0-100).

    Uses FTS5 trigram search for candidate retrieval, then bigram Dice
    similarity for precise ranking.
    """
    if not title:
        return None, 0
    norm = normalize_title(title)
    if not norm:
        return None, 0

    # 1. Exact match
    result = await db.execute(
        select(Movie).where(
            or_(
                Movie.title_cn == title,
                Movie.title_en == title,
            )
        )
    )
    movie = result.scalars().first()
    if movie:
        return movie, 100

    # 2. FTS5 candidate retrieval + similarity scoring
    candidate_ids = await fts_service.search_movie_fts(db, title, limit=30)
    if candidate_ids:
        result = await db.execute(select(Movie).where(Movie.id.in_(candidate_ids)))
        candidates = result.scalars().all()
    else:
        # FTS index may be empty/out of sync — fall back to full-table scan
        result = await db.execute(select(Movie))
        candidates = result.scalars().all()

    best: Movie | None = None
    best_score = 0
    for m in candidates:
        titles = [m.title_cn, m.title_en, *(m.aliases or [])]
        score = max((similarity_score(norm, t) for t in titles if t), default=0)
        if score > best_score:
            best_score = score
            best = m

    if best_score >= FUZZY_THRESHOLD:
        return best, best_score
    return None, 0


async def match_audio_work_by_title(db: AsyncSession, title: str) -> tuple[AudioWork | None, int]:
    """Find best matching AudioWork in local DB. Returns (entity, score 0-100).

    Uses FTS5 trigram search for candidate retrieval, then bigram Dice
    similarity for precise ranking. Mirrors :func:`match_movie_by_title`.
    """
    if not title:
        return None, 0
    norm = normalize_title(title)
    if not norm:
        return None, 0

    result = await db.execute(
        select(AudioWork).where(
            or_(
                AudioWork.title_cn == title,
                AudioWork.title_en == title,
            )
        )
    )
    audio = result.scalars().first()
    if audio:
        return audio, 100

    candidate_ids = await fts_service.search_audio_work_fts(db, title, limit=30)
    if candidate_ids:
        result = await db.execute(select(AudioWork).where(AudioWork.id.in_(candidate_ids)))
        candidates = result.scalars().all()
    else:
        result = await db.execute(select(AudioWork))
        candidates = result.scalars().all()

    best: AudioWork | None = None
    best_score = 0
    for a in candidates:
        titles = [a.title_cn, a.title_en, *(a.aliases or [])]
        score = max((similarity_score(norm, t) for t in titles if t), default=0)
        if score > best_score:
            best_score = score
            best = a

    if best_score >= FUZZY_THRESHOLD:
        return best, best_score
    return None, 0


# ---------------------------------------------------------------------------
# Metadata search (delegates to UnifiedMetadataAgent)
# ---------------------------------------------------------------------------


async def search_metadata_via_llm(
    title: str,
    data_source_type: str | None = None,
) -> list[dict]:
    """Search for metadata using the unified metadata agent.

    Delegates to ``UnifiedMetadataAgent.process_title_only()`` for title cleaning
    and metadata search via one selected source.
    Returns a list of candidate dicts (same shape as before) so callers work unchanged.
    """
    from app.services.metadata_agent import get_agent

    try:
        logger.info(
            "[metadata] agent search start title=%r data_source_type=%s",
            title[:160], data_source_type,
        )
        result = await get_agent().process_title_only(title, data_source_type)
    except Exception as e:
        logger.warning("[metadata] Agent search failed for %r: %s", title[:60], e)
        return []

    if not result.found:
        if result.ambiguous and result.ambiguous_candidates:
            return result.ambiguous_candidates
        return []

    candidates: list[dict] = []
    if result.matched_entity:
        # Propagate a work-level ambiguity flag onto the lead candidate so
        # callers (Layer-4 auto-link) can refuse to link a match the agent
        # itself was unsure about.
        lead = dict(result.matched_entity)
        if result.ambiguous:
            lead["ambiguous"] = True
        candidates.append(lead)
    if result.ambiguous and result.ambiguous_candidates:
        candidates.extend(result.ambiguous_candidates)

    logger.info(
        "[metadata] agent search done title=%r data_source_type=%s found=%s candidates=%d error=%s",
        title[:160],
        data_source_type,
        result.found,
        len(candidates),
        result.search_error,
    )
    return candidates


# ---------------------------------------------------------------------------
# Entity upsert helpers
# ---------------------------------------------------------------------------

def _parse_date(val: Any) -> date | None:
    if not val:
        return None
    if isinstance(val, datetime):
        return val.date()
    if isinstance(val, date):
        return val
    s = str(val).strip()
    # Try full date/time formats first so a YYYY-MM-DD string isn't interpreted as YYYY.
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            # %Y-%m-%d consumes 10 characters (4+2+2 + 2 dashes) even though the
            # format string itself is 8 characters. Use the full input slice.
            if len(s) >= len(fmt):
                candidate = s
                # If the string is longer than needed, take only a prefix of the
                # appropriate length for the format.
                if fmt == "%Y-%m-%d":
                    candidate = s[:10]
                else:
                    candidate = s[:19]
                return datetime.strptime(candidate, fmt).date()
        except (ValueError, TypeError):
            continue
    # Year-only: 4-digit string that isn't a longer date
    try:
        if len(s) == 4 and s.isdigit():
            return date(int(s), 1, 1)
    except (ValueError, TypeError):
        pass
    return None


async def find_series_by_external_id(db: AsyncSession, data: dict) -> TVSeries | None:
    """Look up an existing TVSeries by the candidate's canonical external_id.

    Cross-table guard: a "movie" verdict for an external entity that already
    owns a TVSeries row must not spawn a duplicate Movie (and vice versa).
    """
    raw_external_id = data.get("external_id")
    raw_source = data.get("external_source")
    canonical_id = canonicalize_external_id(
        raw_external_id, raw_source, data.get("content_type")
    )
    lookup_ids = {i for i in (canonical_id, raw_external_id) if i}
    if not lookup_ids:
        return None
    stmt = select(TVSeries).where(TVSeries.external_id.in_(lookup_ids))
    lookup_sources = {s for s in (raw_source, "llm_search") if s}
    if lookup_sources:
        stmt = stmt.where(TVSeries.external_source.in_(lookup_sources))
    return (await db.execute(stmt)).scalars().first()


async def find_movie_by_external_id(db: AsyncSession, data: dict) -> Movie | None:
    """Mirror of :func:`find_series_by_external_id` for the movies table."""
    raw_external_id = data.get("external_id")
    raw_source = data.get("external_source")
    canonical_id = canonicalize_external_id(
        raw_external_id, raw_source, data.get("content_type")
    )
    lookup_ids = {i for i in (canonical_id, raw_external_id) if i}
    if not lookup_ids:
        return None
    stmt = select(Movie).where(Movie.external_id.in_(lookup_ids))
    lookup_sources = {s for s in (raw_source, "llm_search") if s}
    if lookup_sources:
        stmt = stmt.where(Movie.external_source.in_(lookup_sources))
    return (await db.execute(stmt)).scalars().first()


def seasons_overwrite_allowed(
    existing_seasons: list | None,
    existing_number_of_seasons: int | None,
    incoming_seasons: list | None,
) -> bool:
    """P2 anti-regression guard for the wikipedia seasons override.

    Overwrite is allowed when (a) no season structure exists yet, (b) the
    incoming data has MORE seasons than existing, or (c) the count is equal
    (structure refresh). It is BLOCKED when the incoming data has FEWER
    seasons than existing - e.g. a zh page whose infobox models the work
    merged ({1: 51}) must not regress a verified 4-season row.
    """
    existing_count = len(existing_seasons or []) or (existing_number_of_seasons or 0)
    if existing_count == 0:
        return True
    return len(incoming_seasons or []) >= existing_count


async def upsert_episodes(db: AsyncSession, series: TVSeries, episode_list: list[dict]) -> int:
    """Idempotently upsert Episode rows from a parsed Wikipedia episode_list.

    Keyed by (series_id, season, episode); existing rows get their title /
    air_date refreshed (when the incoming value is non-null), missing rows
    are inserted. Additive only - extra rows (e.g. manually curated or from
    a source no longer listing them) are never deleted this phase. Returns
    the number of episode entries processed.
    """
    items = [
        e for e in (episode_list or [])
        if e.get("season") is not None and e.get("episode") is not None
    ]
    if not items:
        return 0
    result = await db.execute(select(Episode).where(Episode.series_id == series.id))
    existing = {(r.season, r.episode): r for r in result.scalars().all()}
    for e in items:
        key = (int(e["season"]), int(e["episode"]))
        air_date = _parse_date(e.get("air_date"))
        row = existing.get(key)
        if row is None:
            db.add(Episode(
                series_id=series.id,
                season=key[0],
                episode=key[1],
                title=e.get("title"),
                air_date=air_date,
            ))
        else:
            if e.get("title"):
                row.title = e["title"]
            if air_date:
                row.air_date = air_date
    await db.flush()
    return len(items)


async def _bag_matched_entity_ids(
    db: AsyncSession, work_type: str, work_id: str, data: dict
) -> None:
    """P3: write every external id a matched_entity carries into the identity bag.

    The incoming primary id (``external_source``/``external_id``) plus any
    ``alt_external_ids: [{source, id}]`` (e.g. wikipedia langlink pageids) are
    bagged. The work's PRIMARY ``external_id`` column is never touched here —
    creator-wins; later-discovered ids only enter the bag. Non-registry
    sources (e.g. ``llm_search``) are skipped by ``add_external_id``.
    """
    await add_external_id(
        db, work_type, work_id, data.get("external_source"), data.get("external_id")
    )
    for alt in data.get("alt_external_ids") or []:
        if not isinstance(alt, dict):
            continue
        await add_external_id(db, work_type, work_id, alt.get("source"), alt.get("id"))


async def create_or_update_series_from_external(db: AsyncSession, data: dict) -> TVSeries:
    """Upsert a TVSeries by identity-bag, canonical external_id, then exact title.

    Lookup order (P3):
      1. Identity-bag reverse lookup — any id ever bagged for the work
         (langlink pageids, Exa-fallback ids, ...) converges deterministically.
      2. Legacy ``external_id`` column lookup (canonical + raw shapes) — kept
         for rows written before canonicalization/the bag existed.
      3. Exact case-sensitive match on ``title_cn`` / ``title_en`` /
         ``original_title`` / alt_titles — the strong signal that a fresh
         Exa response describes an already-known work.

    On every successful upsert the incoming id(s) are written into the bag;
    the primary column keeps its creator-wins semantics.
    """
    raw_external_id = data.get("external_id")
    raw_source = data.get("external_source")
    content_type = data.get("content_type")
    canonical_id = canonicalize_external_id(raw_external_id, raw_source, content_type)

    # (1) Identity bag — deterministic cross-source/cross-language convergence.
    series: TVSeries | None = None
    if raw_external_id:
        series = await find_work_by_external_id(db, "series", raw_source, raw_external_id)

    # (2) Legacy column lookup — canonical id preferred, but keep matching
    # legacy rows written before canonicalization existed. ``llm_search`` is a
    # legacy source label kept for compatibility.
    lookup_ids = {i for i in (canonical_id, raw_external_id) if i}
    lookup_sources = {s for s in (raw_source, "llm_search") if s}

    if series is None and lookup_ids:
        stmt = select(TVSeries).where(TVSeries.external_id.in_(lookup_ids))
        if lookup_sources:
            stmt = stmt.where(TVSeries.external_source.in_(lookup_sources))
        result = await db.execute(stmt)
        series = result.scalars().first()

    # (3) Fallback: same work returned with a fresh external_id shape. Match by
    # any of the canonical title columns (case-sensitive; titles are already
    # normalized by upstream extraction). Include season-stripped forms too:
    # stored series titles are base (season-stripped at write time), so an
    # incoming season-suffixed title (e.g. "X 第二季") must be stripped to
    # match the existing base-title row - this is what lets wikipedia/tmdb/exa
    # converge on one series row across sources.
    if series is None:
        raw_candidates = [
            t for t in (
                data.get("title_cn"),
                data.get("title_en"),
                data.get("original_title"),
                # Cross-language page titles from Wikipedia langlinks - the
                # same work's zh/en pages must converge on one row.
                *(data.get("alt_titles") or []),
            ) if t
        ]
        title_candidates = list({
            t for c in raw_candidates for t in (c, strip_season_from_title(c)) if t
        })
        if title_candidates:
            title_result = await db.execute(
                select(TVSeries).where(
                    or_(
                        TVSeries.title_cn.in_(title_candidates),
                        TVSeries.title_en.in_(title_candidates),
                        TVSeries.original_title.in_(title_candidates),
                    )
                )
            )
            series = title_result.scalars().first()

    if series:
        # Migrate legacy/inconsistent identifiers to the canonical form so the
        # next upsert converges even faster.
        if canonical_id:
            series.external_id = canonical_id
        if raw_source and raw_source != "llm_search":
            series.external_source = raw_source
        series.description = data.get("description") or series.description
        if data.get("rating") is not None:
            series.rating = data.get("rating")
        series.original_title = data.get("original_title") or series.original_title
        series.status = data.get("status") or series.status
        # P2 anti-regression guard (wikipedia source only): never overwrite a
        # richer existing season structure with a poorer one (e.g. a merged/
        # manga-modeled zh infobox yielding {1: 51} over a verified 4-season
        # row). tmdb/exa paths are unaffected. number_of_episodes is gated
        # together with seasons because the parser derives it from the same
        # (suspect) season counts.
        seasons_override = True
        if raw_source == "wikipedia" and data.get("seasons"):
            seasons_override = seasons_overwrite_allowed(
                series.seasons, series.number_of_seasons, data["seasons"]
            )
            if not seasons_override:
                logger.warning(
                    "[metadata] wikipedia seasons guard: series %s keeps existing "
                    "%d-season structure; incoming wikipedia seasons=%s rejected",
                    series.id,
                    len(series.seasons or []) or (series.number_of_seasons or 0),
                    data["seasons"],
                )
        if data.get("number_of_episodes") is not None and seasons_override:
            series.number_of_episodes = data.get("number_of_episodes")
        if data.get("number_of_seasons") is not None and seasons_override:
            series.number_of_seasons = data.get("number_of_seasons")
        if data.get("seasons") and seasons_override:
            series.seasons = data["seasons"]
        sd = _parse_date(data.get("start_date"))
        if sd:
            series.start_date = sd
        ed = _parse_date(data.get("end_date"))
        if ed:
            series.end_date = ed
        genres = normalize_genres(data.get("genre"))
        if genres:
            series.genre = genres
        if data.get("title_cn"):
            series.title_cn = series.title_cn or strip_season_from_title(data.get("title_cn"))
        if data.get("title_en"):
            series.title_en = series.title_en or strip_season_from_title(data.get("title_en"))

        existing_titles = {t for t in [series.title_cn, series.title_en, *(series.aliases or [])] if t}
        new_aliases = list(series.aliases or [])
        for t in (
            data.get("title_cn"),
            data.get("title_en"),
            data.get("original_title"),
            *(data.get("alt_titles") or []),
        ):
            if t and t not in existing_titles and t not in new_aliases:
                new_aliases.append(t)
                existing_titles.add(t)
        series.aliases = new_aliases or None

        remote_poster = data.get("poster_url")
        if remote_poster and not (series.poster_url or "").startswith("/posters/"):
            local_url = await download_and_cache_poster(remote_poster)
            series.poster_url = local_url or remote_poster
        series.content_type = "tv"
        apply_is_anime(series, data)
        # P2: wikipedia-sourced episode_list populates Episode rows (additive
        # upsert; seasons/number_of_seasons were already overwritten above).
        if data.get("episode_list"):
            await upsert_episodes(db, series, data["episode_list"])
        # P3: bag every id this entity carries (primary + alt_external_ids).
        await _bag_matched_entity_ids(db, "series", series.id, data)
        return series

    # Create
    remote_poster = data.get("poster_url")
    local_url = await download_and_cache_poster(remote_poster)
    raw_cn = data.get("title_cn")
    raw_en = data.get("title_en") or data.get("original_title")
    title_cn = strip_season_from_title(raw_cn)
    title_en = strip_season_from_title(raw_en)
    aliases: list[str] = []
    # Keep the original (season-suffixed) forms as aliases too, so resources
    # whose title still carries the season can still match via the title index.
    # alt_titles carries the cross-language Wikipedia page titles.
    for t in (
        raw_cn,
        raw_en,
        data.get("original_title"),
        title_cn,
        title_en,
        *(data.get("alt_titles") or []),
    ):
        if t and t not in aliases:
            aliases.append(t)
    series = TVSeries(
        title_cn=title_cn,
        title_en=title_en,
        original_title=data.get("original_title"),
        aliases=aliases or None,
        external_id=canonical_id or raw_external_id,
        external_source=data.get("external_source", "llm_search"),
        description=data.get("description"),
        poster_url=local_url or remote_poster,
        rating=data.get("rating"),
        genre=normalize_genres(data.get("genre")),
        status=data.get("status"),
        number_of_episodes=data.get("number_of_episodes"),
        number_of_seasons=data.get("number_of_seasons"),
        seasons=data.get("seasons") or None,
        start_date=_parse_date(data.get("start_date")),
        end_date=_parse_date(data.get("end_date")),
        content_type="tv",
    )
    apply_is_anime(series, data)
    db.add(series)
    await db.flush()
    if data.get("episode_list"):
        await upsert_episodes(db, series, data["episode_list"])
    # P3: bag every id this entity carries (primary + alt_external_ids).
    await _bag_matched_entity_ids(db, "series", series.id, data)
    return series


async def create_or_update_movie_from_external(db: AsyncSession, data: dict) -> Movie:
    """Upsert a Movie by identity-bag, canonical external_id, then exact title.

    See :func:`create_or_update_series_from_external` for the lookup order and
    identity-bag (P3) rationale.
    """
    raw_external_id = data.get("external_id")
    raw_source = data.get("external_source")
    content_type = data.get("content_type")
    canonical_id = canonicalize_external_id(raw_external_id, raw_source, content_type)

    # (1) Identity bag — deterministic cross-source convergence.
    movie: Movie | None = None
    if raw_external_id:
        movie = await find_work_by_external_id(db, "movie", raw_source, raw_external_id)

    # (2) Legacy column lookup.
    lookup_ids = {i for i in (canonical_id, raw_external_id) if i}
    lookup_sources = {s for s in (raw_source, "llm_search") if s}

    if movie is None and lookup_ids:
        stmt = select(Movie).where(Movie.external_id.in_(lookup_ids))
        if lookup_sources:
            stmt = stmt.where(Movie.external_source.in_(lookup_sources))
        result = await db.execute(stmt)
        movie = result.scalars().first()

    if movie is None:
        title_candidates = [
            t for t in (
                data.get("title_cn"),
                data.get("title_en"),
                data.get("original_title"),
                *(data.get("alt_titles") or []),
            ) if t
        ]
        if title_candidates:
            title_result = await db.execute(
                select(Movie).where(
                    or_(
                        Movie.title_cn.in_(title_candidates),
                        Movie.title_en.in_(title_candidates),
                        Movie.original_title.in_(title_candidates),
                    )
                )
            )
            movie = title_result.scalars().first()

    if movie:
        if canonical_id:
            movie.external_id = canonical_id
        if raw_source and raw_source != "llm_search":
            movie.external_source = raw_source
        movie.description = data.get("description") or movie.description
        if data.get("rating") is not None:
            movie.rating = data.get("rating")
        movie.original_title = data.get("original_title") or movie.original_title
        movie.status = data.get("status") or movie.status
        rd = _parse_date(data.get("release_date"))
        if rd:
            movie.release_date = rd
        if data.get("runtime") is not None:
            movie.runtime = data.get("runtime")
        genres = normalize_genres(data.get("genre"))
        if genres:
            movie.genre = genres
        if data.get("title_cn"):
            movie.title_cn = movie.title_cn or data.get("title_cn")
        if data.get("title_en"):
            movie.title_en = movie.title_en or data.get("title_en")

        existing_titles = {t for t in [movie.title_cn, movie.title_en, *(movie.aliases or [])] if t}
        new_aliases = list(movie.aliases or [])
        for t in (
            data.get("title_cn"),
            data.get("title_en"),
            data.get("original_title"),
            *(data.get("alt_titles") or []),
        ):
            if t and t not in existing_titles and t not in new_aliases:
                new_aliases.append(t)
                existing_titles.add(t)
        movie.aliases = new_aliases or None

        remote_poster = data.get("poster_url")
        if remote_poster and not (movie.poster_url or "").startswith("/posters/"):
            local_url = await download_and_cache_poster(remote_poster)
            movie.poster_url = local_url or remote_poster
        movie.content_type = "movie"
        apply_is_anime(movie, data)
        # P3: bag every id this entity carries (primary + alt_external_ids).
        await _bag_matched_entity_ids(db, "movie", movie.id, data)
        return movie

    remote_poster = data.get("poster_url")
    local_url = await download_and_cache_poster(remote_poster)
    title_cn = data.get("title_cn")
    title_en = data.get("title_en") or data.get("original_title")
    aliases: list[str] = []
    for t in (title_cn, title_en, data.get("original_title"), *(data.get("alt_titles") or [])):
        if t and t not in aliases:
            aliases.append(t)
    movie = Movie(
        title_cn=title_cn,
        title_en=title_en,
        original_title=data.get("original_title"),
        aliases=aliases or None,
        external_id=canonical_id or raw_external_id,
        external_source=data.get("external_source", "llm_search"),
        description=data.get("description"),
        poster_url=local_url or remote_poster,
        rating=data.get("rating"),
        genre=normalize_genres(data.get("genre")),
        status=data.get("status"),
        release_date=_parse_date(data.get("release_date")),
        runtime=data.get("runtime"),
        content_type="movie",
    )
    apply_is_anime(movie, data)
    db.add(movie)
    await db.flush()
    # P3: bag every id this entity carries (primary + alt_external_ids).
    await _bag_matched_entity_ids(db, "movie", movie.id, data)
    return movie


async def create_or_update_audio_work_from_external(db: AsyncSession, data: dict) -> AudioWork:
    """Upsert an AudioWork by canonicalized external_id, then by exact title.

    Mirrors :func:`create_or_update_movie_from_external`. ``data["content_type"]``
    carries the sub-kind (asmr / music / drama_cd / radio / other) and is
    preserved on the entity.
    """
    raw_external_id = data.get("external_id")
    raw_source = data.get("external_source")
    content_type = data.get("content_type") or "other"
    canonical_id = canonicalize_external_id(raw_external_id, raw_source, content_type)

    lookup_ids = {i for i in (canonical_id, raw_external_id) if i}
    lookup_sources = {s for s in (raw_source, "llm_search") if s}

    audio: AudioWork | None = None
    if lookup_ids:
        stmt = select(AudioWork).where(AudioWork.external_id.in_(lookup_ids))
        if lookup_sources:
            stmt = stmt.where(AudioWork.external_source.in_(lookup_sources))
        result = await db.execute(stmt)
        audio = result.scalars().first()

    if audio is None:
        title_candidates = [
            t for t in (
                data.get("title_cn"),
                data.get("title_en"),
                data.get("original_title"),
            ) if t
        ]
        if title_candidates:
            title_result = await db.execute(
                select(AudioWork).where(
                    or_(
                        AudioWork.title_cn.in_(title_candidates),
                        AudioWork.title_en.in_(title_candidates),
                        AudioWork.original_title.in_(title_candidates),
                    )
                )
            )
            audio = title_result.scalars().first()

    if audio:
        if canonical_id:
            audio.external_id = canonical_id
        if raw_source and raw_source != "llm_search":
            audio.external_source = raw_source
        audio.description = data.get("description") or audio.description
        if data.get("rating") is not None:
            audio.rating = data.get("rating")
        audio.original_title = data.get("original_title") or audio.original_title
        audio.status = data.get("status") or audio.status
        rd = _parse_date(data.get("release_date"))
        if rd:
            audio.release_date = rd
        if data.get("runtime") is not None:
            audio.runtime = data.get("runtime")
        genres = normalize_genres(data.get("genre"))
        if genres:
            audio.genre = genres
        if data.get("title_cn"):
            audio.title_cn = audio.title_cn or data.get("title_cn")
        if data.get("title_en"):
            audio.title_en = audio.title_en or data.get("title_en")
        if data.get("content_type"):
            audio.content_type = data.get("content_type")

        existing_titles = {t for t in [audio.title_cn, audio.title_en, *(audio.aliases or [])] if t}
        new_aliases = list(audio.aliases or [])
        for t in (data.get("title_cn"), data.get("title_en"), data.get("original_title")):
            if t and t not in existing_titles and t not in new_aliases:
                new_aliases.append(t)
                existing_titles.add(t)
        audio.aliases = new_aliases or None

        remote_poster = data.get("poster_url")
        if remote_poster and not (audio.poster_url or "").startswith("/posters/"):
            local_url = await download_and_cache_poster(remote_poster)
            audio.poster_url = local_url or remote_poster
        return audio

    remote_poster = data.get("poster_url")
    local_url = await download_and_cache_poster(remote_poster)
    title_cn = data.get("title_cn")
    title_en = data.get("title_en") or data.get("original_title")
    aliases: list[str] = []
    for t in (title_cn, title_en, data.get("original_title")):
        if t and t not in aliases:
            aliases.append(t)
    audio = AudioWork(
        title_cn=title_cn,
        title_en=title_en,
        original_title=data.get("original_title"),
        aliases=aliases or None,
        external_id=canonical_id or raw_external_id,
        external_source=data.get("external_source", "llm_search"),
        description=data.get("description"),
        poster_url=local_url or remote_poster,
        rating=data.get("rating"),
        genre=normalize_genres(data.get("genre")),
        status=data.get("status"),
        release_date=_parse_date(data.get("release_date")),
        runtime=data.get("runtime"),
        content_type=content_type,
    )
    db.add(audio)
    await db.flush()
    return audio


# ---------------------------------------------------------------------------
# Work metadata refresh (works-page "fill missing fields" action)
# ---------------------------------------------------------------------------


def _first_present(*values: Any) -> Any:
    """Return the first value that is not None/empty, else None."""
    for v in values:
        if v not in (None, "", [], ()):
            return v
    return None


def _safe_float(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _safe_int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


async def refresh_work_metadata(
    db: AsyncSession,
    work_id: str,
    content_type: str,
    source: str | None,
) -> dict:
    """Re-search metadata for an existing TVSeries/Movie and fill missing fields.

    Uses the work's existing titles as the search query against *source* (one
    of the external metadata sources). Only fields that are currently empty on
    the work are filled — existing user/agent values are preserved. Posters are
    downloaded and cached locally like the initial ingestion path.

    Returns a summary dict: ``{found, filled, source, message}``.
    """
    is_movie = (content_type or "").lower() == "movie"
    work = await db.get(Movie if is_movie else TVSeries, work_id)
    if not work:
        return {"found": False, "filled": [], "source": source, "message": "work not found"}

    search_title = _first_present(work.title_en, work.title_cn, work.original_title)
    if not search_title:
        return {
            "found": True,
            "filled": [],
            "source": source,
            "message": "no title available to search",
        }

    candidates = await search_metadata_via_llm(search_title, source)
    if not candidates:
        return {
            "found": True,
            "filled": [],
            "source": source,
            "message": "no candidates returned by source",
        }

    # LLM variance: the candidates list may carry season-ambiguity entries
    # ({"season": n}) or stray non-dict items — neither is a work candidate.
    candidates = [
        c for c in candidates
        if isinstance(c, dict)
        and any(c.get(k) for k in ("title_cn", "title_en", "original_title", "canonical_name"))
    ]
    if not candidates:
        return {
            "found": True,
            "filled": [],
            "source": source,
            "message": "no usable work candidates returned by source",
        }

    # Prefer a candidate whose content_type matches the work.
    best = next((c for c in candidates if c.get("content_type") == content_type), None)
    if best is None:
        best = candidates[0]

    filled: list[str] = []

    def fill(attr: str, key: str, cast: Any = lambda x: x) -> None:
        cur = getattr(work, attr)
        if cur in (None, "", [], ()):
            val = best.get(key)
            if val not in (None, ""):
                setattr(work, attr, cast(val))
                filled.append(attr)

    fill("description", "description")
    fill("rating", "rating", _safe_float)
    fill("status", "status")
    fill("original_title", "original_title")
    fill("title_cn", "title_cn")
    fill("title_en", "title_en")

    if not work.genre:
        g = normalize_genres(best.get("genre"))
        if g:
            work.genre = g
            filled.append("genre")

    if is_movie:
        fill("release_date", "release_date", _parse_date)
        fill("runtime", "runtime", _safe_int)
    else:
        fill("number_of_episodes", "number_of_episodes", _safe_int)
        fill("number_of_seasons", "number_of_seasons", _safe_int)
        fill("start_date", "start_date", _parse_date)
        fill("end_date", "end_date", _parse_date)

    # Poster: download + cache, like the initial ingestion path.
    remote_poster = best.get("poster_url")
    if remote_poster and not (work.poster_url or "").startswith("/posters/"):
        local_url = await download_and_cache_poster(remote_poster)
        work.poster_url = local_url or remote_poster
        filled.append("poster_url")

    if not work.external_id and best.get("external_id"):
        work.external_id = best["external_id"]
        filled.append("external_id")
    if not work.external_source and best.get("external_source"):
        work.external_source = best["external_source"]
        filled.append("external_source")

    await db.flush()
    await db.commit()

    label = best.get("title_cn") or best.get("title_en") or best.get("original_title") or ""
    return {
        "found": True,
        "filled": filled,
        "source": source,
        "message": f"matched: {label}" if label else "matched",
        "candidate": {
            "title_cn": best.get("title_cn"),
            "title_en": best.get("title_en"),
            "external_id": best.get("external_id"),
            "external_source": best.get("external_source"),
        },
    }


# ---------------------------------------------------------------------------
# Layer-3 auto-link guards
# ---------------------------------------------------------------------------


def _year_mismatch(title_year: int | None, work_date: Any) -> bool:
    """True when the title-parsed year conflicts with the work's year.

    ``work_date`` may be a ``date``/``datetime`` or an ISO string (or None —
    no evidence either way → not a mismatch). A ±1 year slack absorbs
    broadcast-year vs production-year differences.
    """
    if not title_year or not work_date:
        return False
    if isinstance(work_date, (date, datetime)):
        work_year = work_date.year
    else:
        m = re.match(r"\s*(\d{4})", str(work_date))
        work_year = int(m.group(1)) if m else None
    if work_year is None:
        return False
    return abs(title_year - work_year) > 1


async def _find_same_title_works(db: AsyncSession, search_title: str) -> list[dict]:
    """Local works sharing the normalized search title exactly (>1 = collision).

    Same-title works (remakes, reboots like 攻壳机动队 vs 攻壳机动队 2026) must
    not be top-1 auto-linked on similarity alone, and the metadata agent needs
    the list injected into its prompt to pick the right one. Cheap exact-match
    query (no fuzzy scan): fetch rows whose title_cn/title_en equals the raw
    search title or its normalized form, then compare normalized titles in
    Python.

    Returns a list of ``{id, title_cn, title_en, year, content_type,
    number_of_seasons}`` dicts — empty unless MORE THAN ONE work collides, so
    truthiness matches the old bool semantics for the Layer-3 caller.
    """
    norm = normalize_title(search_title)
    if not norm:
        return []
    candidates = {search_title, norm}
    works: list[dict] = []
    seen: set[str] = set()
    for model, date_attr, ctype in (
        (TVSeries, "start_date", "tv"),
        (Movie, "release_date", "movie"),
    ):
        result = await db.execute(
            select(model).where(
                or_(
                    model.title_cn.in_(candidates),
                    model.title_en.in_(candidates),
                )
            )
        )
        for row in result.scalars().all():
            if row.id in seen:
                continue
            if normalize_title(row.title_cn) == norm or normalize_title(row.title_en) == norm:
                seen.add(row.id)
                d = getattr(row, date_attr, None)
                works.append({
                    "id": row.id,
                    "title_cn": row.title_cn,
                    "title_en": row.title_en,
                    "year": d.year if d else None,
                    "content_type": ctype,
                    "number_of_seasons": getattr(row, "number_of_seasons", None),
                })
    return works if len(works) > 1 else []


def format_same_title_works_context(works: list[dict]) -> str:
    """Prompt fragment listing local same-title works for the metadata agent.

    Injected when ≥2 local works collide on the search title — the actual
    error mode is linking to the wrong EXISTING local work, so the agent is
    told the candidates and asked to use the title's year to pick correctly.
    """
    items = []
    for w in works:
        title = w.get("title_cn") or w.get("title_en") or "?"
        bits = [str(w["year"]) if w.get("year") else "年份未知", w["content_type"]]
        if w["content_type"] == "tv" and w.get("number_of_seasons"):
            bits.append(f"{w['number_of_seasons']} seasons")
        items.append(f"{title} ({', '.join(bits)})")
    return (
        f"本地库存在同名作品: [{'; '.join(items)}]; 结合标题年份选择正确作品"
    )


# ---------------------------------------------------------------------------
# Main entry points
# ---------------------------------------------------------------------------

async def fetch_and_link_metadata(db: AsyncSession, resource: Any, channel: Any) -> None:
    """Match metadata for a newly-created FileResource and set its FKs.

    Implements the 4-layer matching strategy from AGENTS.md.
    """
    # Layer 1: already linked
    if resource.series_id or resource.movie_id:
        return

    async def _reconcile_with_series() -> None:
        """Episode reconciliation for agent-free link paths: uses the linked
        series' persisted per-season counts (absolute-numbered releases like
        "第四季 - 89" would otherwise never be converted).

        Verified season default (never guess): after reconciliation, a linked
        resource whose season is still unknown is handled by the shared
        ``resolve_missing_season`` helper — set to 1 ONLY when the series
        provably has a single season; a multi-season (or unknown) series means
        the season can't be verified — the resource is marked season-uncertain
        (``episode_confidence="ambiguous"``) and routed to a "季号不确定"
        PendingDecision downstream. Batch resources are excluded (a 合集
        doesn't need a verified single season number to dispatch), and
        movie-linked resources are untouched."""
        if not resource.series_id:
            return
        from app.models.series import TVSeries
        from app.services.metadata_episode_reconcile import (
            apply_episode_reconcile,
            resolve_missing_season,
            seasons_map_from_list,
        )
        series_row = await db.get(TVSeries, resource.series_id)
        if series_row is None:
            return
        if series_row.seasons:
            apply_episode_reconcile(resource, seasons_map_from_list(series_row.seasons))
        resolve_missing_season(resource, {
            "number_of_seasons": series_row.number_of_seasons,
            "seasons": series_row.seasons,
        })

    # Layer 2: ChannelRawTitleMapping
    # Primary lookup: by normalized search_title_key (handles episode/resolution variations)
    search_key = normalize_title(extract_search_title(resource))
    mapping = None
    if search_key:
        mapping_result = await db.execute(
            select(ChannelRawTitleMapping).where(
                ChannelRawTitleMapping.channel_id == channel.id,
                ChannelRawTitleMapping.search_title_key == search_key,
            )
        )
        mapping = mapping_result.scalars().first()
    # Fallback: by exact raw_title (compatibility with pre-search_key mappings)
    if not mapping:
        mapping_result = await db.execute(
            select(ChannelRawTitleMapping).where(
                ChannelRawTitleMapping.channel_id == channel.id,
                ChannelRawTitleMapping.raw_title == resource.title_raw,
            )
        )
        mapping = mapping_result.scalars().first()
    if mapping:
        if mapping.series_id:
            resource.series_id = mapping.series_id
            resource.movie_id = None
        elif mapping.movie_id:
            resource.movie_id = mapping.movie_id
            resource.series_id = None
        if mapping.search_title_override:
            resource.search_title = mapping.search_title_override
        resource.metadata_matched_at = utcnow()
        await _reconcile_with_series()
        await classify_is_anime_post_link(db, channel, resource)
        return

    # Layer 3: local match
    search_title = resource.search_title or extract_search_title(resource)
    if not search_title:
        _record_unmatched_attempt(resource, "not_found")
        return

    series, s_ratio = await match_series_by_title(db, search_title)
    movie, m_ratio = await match_movie_by_title(db, search_title)

    # Auto-link only at >=85 ratio — guarded:
    #  * resource.title_year conflicts with the work's start/release year
    #    (beyond ±1) → likely a same-title remake (攻壳机动队 2026), don't
    #    auto-link;
    #  * the normalized search title exactly matches MORE THAN ONE local work
    #    → ambiguous top-1, don't auto-link.
    # Blocked matches fall through to Layer 4 (metadata-source search).
    title_year = getattr(resource, "title_year", None)
    collision: list | None = None  # lazy — only queried when a candidate qualifies

    async def _auto_link_blocked(work_date: Any) -> bool:
        nonlocal collision
        if _year_mismatch(title_year, work_date):
            return True
        if collision is None:
            collision = await _find_same_title_works(db, search_title)
        return bool(collision)

    if series and s_ratio >= AUTO_LINK_THRESHOLD and (movie is None or s_ratio >= m_ratio):
        if not await _auto_link_blocked(getattr(series, "start_date", None)):
            resource.series_id = series.id
            resource.metadata_matched_at = utcnow()
            if not series.poster_url or not (series.poster_url or "").startswith("/posters/"):
                pass  # poster already handled if set
            await _reconcile_with_series()
            await classify_is_anime_post_link(db, channel, resource)
            return
    if movie and m_ratio >= AUTO_LINK_THRESHOLD and (series is None or m_ratio > s_ratio):
        if not await _auto_link_blocked(getattr(movie, "release_date", None)):
            resource.movie_id = movie.id
            resource.metadata_matched_at = utcnow()
            await classify_is_anime_post_link(db, channel, resource)
            return

    # NOTE: 70-84 matches (and ≥85 matches blocked by the guards above) are
    # skipped and fall through to the LLM layer.

    # Layer 4: selected-source metadata search
    if not channel.metadata_agent_enabled:
        _record_unmatched_attempt(resource, "not_found")
        return

    try:
        from app.services.metadata_agent import resolve_metadata_source

        # Respect the channel's configured source (e.g. jina) instead of
        # hardcoding the default - otherwise a Jina channel's per-resource
        # refresh would silently run the Exa agent.
        data_source_type = resolve_metadata_source(getattr(channel, "metadata_source", None))
        results = await search_metadata_via_llm(search_title, data_source_type)
    except Exception as e:
        logger.warning("[metadata] LLM search failed for %r: %s", search_title[:60], e)
        _record_unmatched_attempt(resource, "transient")
        return

    if not results:
        _record_unmatched_attempt(resource, "not_found")
        return
    best = results[0]
    # Work-level ambiguous verdict (the agent itself flagged the match as
    # uncertain): never auto-link — record not_found so the resource stays
    # manually linkable instead of binding to a guessed work.
    if best.get("ambiguous"):
        _record_unmatched_attempt(resource, "not_found")
        return
    try:
        if best.get("content_type") == "movie":
            movie_entity = await create_or_update_movie_from_external(db, best)
            resource.movie_id = movie_entity.id
            resource.series_id = None
            from app.services.collection_service import link_movie_collection
            await link_movie_collection(db, movie_entity)
        else:
            series_entity = await create_or_update_series_from_external(db, best)
            resource.series_id = series_entity.id
            resource.movie_id = None
        resource.metadata_matched_at = utcnow()
        await _reconcile_with_series()
        await classify_is_anime_post_link(db, channel, resource)
    except Exception as e:
        logger.warning("[metadata] Failed to link via LLM for %r: %s", search_title[:60], e)
        _record_unmatched_attempt(resource, "transient")


async def manual_search_metadata(
    db: AsyncSession,
    search_title: str,
    content_type: str,
    data_source_type: str | None = None,
) -> list[dict]:
    """Search for metadata candidates. No persistence.

    When ``data_source_type == "local"``, searches the local TVSeries/Movie
    library via FTS5 instead of calling the LLM agent. This allows users to
    match resources against already-known works without external API calls.
    """
    logger.info(
        "[metadata] manual_search start title=%r content_type=%s data_source_type=%s",
        search_title[:160], content_type, data_source_type,
    )

    # Local library data source — search existing TVSeries/Movie via FTS5
    if data_source_type == "local":
        results = await _search_local_library(db, search_title, content_type)
        logger.info(
            "[metadata] manual_search (local) done title=%r candidates=%d",
            search_title[:160], len(results),
        )
        return results

    results = await search_metadata_via_llm(search_title, data_source_type)
    normalized: list[dict] = []
    for result in results:
        item = dict(result)
        if item.get("content_type") not in ("tv", "movie"):
            item["content_type"] = content_type if content_type in ("tv", "movie") else "tv"
        normalized.append(item)
    results = normalized
    if content_type in ("tv", "movie"):
        # Prefer content type but don't strictly filter — return all candidates
        preferred = [r for r in results if r.get("content_type") == content_type]
        if preferred:
            logger.info(
                "[metadata] manual_search done title=%r preferred_candidates=%d total_candidates=%d",
                search_title[:160], len(preferred), len(results),
            )
            return preferred
    logger.info(
        "[metadata] manual_search done title=%r candidates=%d",
        search_title[:160], len(results),
    )
    return results


async def _search_local_library(
    db: AsyncSession,
    search_title: str,
    content_type: str,
) -> list[dict]:
    """Search the local TVSeries/Movie library via FTS5.

    Returns candidates in the same dict shape as LLM search results so the
    frontend can reuse the same selection UI.
    """
    results: list[dict] = []
    norm = normalize_title(search_title)

    if content_type != "movie":
        # Search TV series
        candidate_ids = await fts_service.search_series_fts(db, search_title, limit=20)
        if candidate_ids:
            from sqlalchemy import select as sa_select
            res = await db.execute(sa_select(TVSeries).where(TVSeries.id.in_(candidate_ids)))
            for s in res.scalars().all():
                titles = [s.title_cn, s.title_en, *(s.aliases or [])]
                score = max((similarity_score(norm, t) for t in titles if t), default=0)
                if score < FUZZY_THRESHOLD:
                    continue
                results.append({
                    "content_type": "tv",
                    "title_cn": s.title_cn,
                    "title_en": s.title_en,
                    "original_title": s.original_title,
                    "external_id": s.external_id,
                    "external_source": s.external_source or "local_match",
                    "description": s.description,
                    "poster_url": s.poster_url,
                    "rating": s.rating,
                    "genre": s.genre,
                    "status": s.status,
                    "content_type_detail": "tv",
                    "_local_id": s.id,
                    "_score": score,
                })

    if content_type != "tv":
        # Search movies
        candidate_ids = await fts_service.search_movie_fts(db, search_title, limit=20)
        if candidate_ids:
            from sqlalchemy import select as sa_select
            res = await db.execute(sa_select(Movie).where(Movie.id.in_(candidate_ids)))
            for m in res.scalars().all():
                titles = [m.title_cn, m.title_en, *(m.aliases or [])]
                score = max((similarity_score(norm, t) for t in titles if t), default=0)
                if score < FUZZY_THRESHOLD:
                    continue
                results.append({
                    "content_type": "movie",
                    "title_cn": m.title_cn,
                    "title_en": m.title_en,
                    "original_title": m.original_title,
                    "external_id": m.external_id,
                    "external_source": m.external_source or "local_match",
                    "description": m.description,
                    "poster_url": m.poster_url,
                    "rating": m.rating,
                    "genre": m.genre,
                    "status": m.status,
                    "content_type_detail": "movie",
                    "_local_id": m.id,
                    "_score": score,
                })

    # Sort by score descending
    results.sort(key=lambda r: r.get("_score", 0), reverse=True)
    return results


async def invalidate_metadata_cache_for_external_id(
    db: AsyncSession, external_id: str | None
) -> int:
    """Drop MetadataCache verdicts whose matched entity is ``external_id``.

    Called after a manual (re)link: the user has just overruled the automatic
    classification, so any cached verdict pointing at the same external
    entity - e.g. a stale "movie" verdict for a work the user re-linked as a
    series - must not be served to future resources. The cache table is small
    by design, so a full scan beats dialect-specific JSON-path queries.
    """
    if not external_id:
        return 0
    from app.models.metadata_cache import MetadataCache

    rows = (await db.execute(select(MetadataCache))).scalars().all()
    removed = 0
    for row in rows:
        me = (row.metadata_json or {}).get("matched_entity") or {}
        if me.get("external_id") == external_id:
            await db.delete(row)
            removed += 1
    if removed:
        logger.info(
            "[metadata] invalidated %d cached verdict(s) for external_id=%r after manual link",
            removed, external_id,
        )
    return removed


async def manual_link_metadata(
    db: AsyncSession,
    resource: Any,
    channel: Any,
    selected_result: dict,
) -> TVSeries | Movie:
    """Manually link a resource to user-selected metadata.

    Creates/updates the entity, sets resource FKs, upserts the
    ChannelRawTitleMapping so future identical titles auto-link.
    """
    if selected_result.get("content_type") == "movie":
        entity = await create_or_update_movie_from_external(db, selected_result)
        resource.movie_id = entity.id
        resource.series_id = None
        series_id = None
        movie_id = entity.id
        content_type = "movie"
        from app.services.collection_service import link_movie_collection
        await link_movie_collection(db, entity)
    else:
        entity = await create_or_update_series_from_external(db, selected_result)
        resource.series_id = entity.id
        resource.movie_id = None
        series_id = entity.id
        movie_id = None
        content_type = "tv"
        # Manual relink bypasses _apply_to_resource: run the same episode
        # reconciliation + verified season rule (resolve_missing_season)
        # against the freshly-upserted series' seasons evidence, so a
        # season-less resource doesn't keep a guessed/empty season.
        from app.services.metadata_episode_reconcile import (
            apply_episode_reconcile,
            resolve_missing_season,
            seasons_map_from_list,
        )
        if entity.seasons:
            apply_episode_reconcile(resource, seasons_map_from_list(entity.seasons))
        resolve_missing_season(resource, {
            "number_of_seasons": entity.number_of_seasons,
            "seasons": entity.seasons,
        })

    resource.metadata_matched_at = utcnow()

    # Upsert ChannelRawTitleMapping
    # Use search_title_key so future resources from the same work (different
    # episode/resolution) also auto-link.
    search_key = normalize_title(extract_search_title(resource))
    if search_key:
        existing = await db.execute(
            select(ChannelRawTitleMapping).where(
                ChannelRawTitleMapping.channel_id == channel.id,
                ChannelRawTitleMapping.search_title_key == search_key,
            )
        )
        mapping = existing.scalars().first()
        if mapping:
            mapping.series_id = series_id
            mapping.movie_id = movie_id
            mapping.content_type = content_type
        else:
            mapping = ChannelRawTitleMapping(
                channel_id=channel.id,
                raw_title=resource.title_raw,
                search_title_key=search_key,
                content_type=content_type,
                series_id=series_id,
                movie_id=movie_id,
            )
            db.add(mapping)
    else:
        # Fallback: use raw_title as key when extraction yields nothing
        existing = await db.execute(
            select(ChannelRawTitleMapping).where(
                ChannelRawTitleMapping.channel_id == channel.id,
                ChannelRawTitleMapping.raw_title == resource.title_raw,
            )
        )
        mapping = existing.scalars().first()
        if mapping:
            mapping.series_id = series_id
            mapping.movie_id = movie_id
            mapping.content_type = content_type
        else:
            mapping = ChannelRawTitleMapping(
                channel_id=channel.id,
                raw_title=resource.title_raw,
                search_title_key=resource.title_raw,
                content_type=content_type,
                series_id=series_id,
                movie_id=movie_id,
            )
            db.add(mapping)

    await db.flush()

    # The user just overruled the automatic classification for this work;
    # purge any cached verdicts for the same external entity so future
    # resources don't inherit the overruled (e.g. wrong-type) result.
    await invalidate_metadata_cache_for_external_id(db, selected_result.get("external_id"))
    return entity
