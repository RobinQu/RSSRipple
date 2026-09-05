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
from app.models.work_collection import WorkCollection
from app.services import fts as fts_service
from app.services.anime_signals import apply_is_anime
from app.services.external_ids import add_external_id, find_work_by_external_id
from app.services.genre_registry import normalize_genres
from app.services.metadata_episode_reconcile import (
    _RECONCILE_TOLERANCE,
    apply_episode_reconcile,
    is_unsplit_legacy_series,
    resolve_missing_work,
    season_evidence_from_series,
    seasons_map_for_work,
    verified_season_count,
)
from app.services.metadata_source_registry import (
    REGISTRY_SOURCES,
    canonicalize_external_id,  # noqa: F401
    granularity_of,
    make_season_identity,
    parse_wikipedia_id,
    qualify_wikipedia_id,
    split_season_identity,
    wikipedia_match_keys,
)
from app.services.resource_parser import season_from_title, strip_season_from_title
from app.services.text_normalizer import normalize_title, similarity_score
from app.utils.time import utcnow

logger = logging.getLogger(__name__)

FUZZY_THRESHOLD = 70
AUTO_LINK_THRESHOLD = 85

# Fields a human may edit through the work detail edit form. Everything else
# (canonical_name / wikipedia_* / seasons / collection_id / search_text /
# timestamps) is system-managed.
# A work's ``manually_edited_fields`` list holds the subset of these the user
# touched; automatic scans (upsert + refresh) must not overwrite them.
MANUAL_EDITABLE_FIELDS: frozenset[str] = frozenset({
    "title_cn", "title_en", "original_title", "aliases", "description",
    "poster_url", "rating", "genre", "status", "is_anime",
    "number_of_episodes", "number_of_seasons", "start_date", "end_date",
    "release_date", "runtime",
    "content_type", "external_id", "external_source",
})


def manually_edited_fields(work: Any) -> set[str]:
    """The set of fields a user has manually edited on ``work``."""
    return set(getattr(work, "manually_edited_fields", None) or [])


def field_manually_edited(work: Any, field: str) -> bool:
    """True when ``field`` was manually edited and must not be auto-written."""
    return field in manually_edited_fields(work)


def mark_manually_edited(work: Any, fields: dict) -> None:
    """Record which of ``fields`` the user edited manually on ``work``.

    Called by the series/movie PUT handlers: every manually-editable field
    present in the update payload (explicitly sent) is added to the work's
    ``manually_edited_fields`` list so automatic scans stop overwriting it.
    """
    edited = [k for k in fields if k in MANUAL_EDITABLE_FIELDS]
    if not edited:
        return
    merged = sorted(manually_edited_fields(work) | set(edited))
    work.manually_edited_fields = merged


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
    if work is not None and work.is_anime is not True and not field_manually_edited(work, "is_anime"):
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
    if field_manually_edited(work, "is_anime"):
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
    fallback_sources: list[str] | None = None,
    *,
    season_hint: int | None = None,
) -> list[dict]:
    """Search for metadata using the unified metadata agent.

    Delegates to ``UnifiedMetadataAgent.process_title_only()`` for title cleaning
    and metadata search via one selected source.
    Returns a list of candidate dicts (same shape as before) so callers work unchanged.

    ``season_hint`` is the season number of the work being refreshed; it keeps
    season-granular sources (bangumi) from matching the season-1 entry for a
    season>1 work.
    """
    from app.services.metadata_agent import get_agent

    try:
        logger.info(
            "[metadata] agent search start title=%r data_source_type=%s",
            title[:160], data_source_type,
        )
        result = await get_agent().process_title_only(
            title, data_source_type,
            fallback_sources=fallback_sources, season_hint=season_hint,
        )
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
    raw_external_id = _qualify_incoming_wikipedia_id(data)
    raw_source = data.get("external_source")
    canonical_id = canonicalize_external_id(
        raw_external_id, raw_source, data.get("content_type")
    )
    lookup_ids = {i for i in (canonical_id, raw_external_id) if i}
    if not lookup_ids:
        return None
    stmt = select(TVSeries).where(_external_id_match(TVSeries.external_id, lookup_ids))
    lookup_sources = {s for s in (raw_source, "llm_search") if s}
    if lookup_sources:
        stmt = stmt.where(TVSeries.external_source.in_(lookup_sources))
    return (await db.execute(stmt)).scalars().first()


async def find_movie_by_external_id(db: AsyncSession, data: dict) -> Movie | None:
    """Mirror of :func:`find_series_by_external_id` for the movies table."""
    raw_external_id = _qualify_incoming_wikipedia_id(data)
    raw_source = data.get("external_source")
    canonical_id = canonicalize_external_id(
        raw_external_id, raw_source, data.get("content_type")
    )
    lookup_ids = {i for i in (canonical_id, raw_external_id) if i}
    if not lookup_ids:
        return None
    stmt = select(Movie).where(_external_id_match(Movie.external_id, lookup_ids))
    lookup_sources = {s for s in (raw_source, "llm_search") if s}
    if lookup_sources:
        stmt = stmt.where(Movie.external_source.in_(lookup_sources))
    return (await db.execute(stmt)).scalars().first()


def _qualify_incoming_wikipedia_id(data: dict) -> str | None:
    """Upgrade a bare ``wikipedia:{pageid}`` to ``wikipedia:{lang}:{pageid}``.

    The lang is taken from the matched page's ``wikipedia_url`` host — this
    covers paths (ReAct, cache replays) where the finalize JSON carries the
    pageid but not the edition. Non-wikipedia / already-qualified / slug ids
    pass through unchanged.
    """
    raw = data.get("external_id")
    if raw and (data.get("external_source") or "").strip().lower() == "wikipedia":
        qualified = qualify_wikipedia_id(raw, wikipedia_url=data.get("wikipedia_url"))
        if qualified != raw:
            data["external_id"] = qualified
        return qualified
    return raw


def _external_id_match(column, lookup_ids: set[str]):
    """Column filter honoring both wikipedia id storage forms.

    A qualified incoming id also matches legacy bare ``wikipedia:{pid}``
    rows exactly; a bare incoming id additionally LIKE-matches any qualified
    ``wikipedia:{lang}:{pid}`` row (pageids are digits-only, so the LIKE
    pattern holds no user-controlled wildcards).
    """
    ids: set[str] = set()
    likes: list[str] = []
    for i in lookup_ids:
        keys, like = wikipedia_match_keys(i)
        ids.update(keys)
        if like:
            likes.append(like)
    conds = [column.in_(ids)]
    conds.extend(column.like(p) for p in likes)
    return or_(*conds)


def _merge_primary_external_id(existing: str | None, incoming: str) -> str:
    """Resolve the primary external_id when an upsert re-matches a work.

    Default: the incoming canonical form wins (migrates legacy id shapes).
    Exception: two wikipedia numeric ids with DIFFERENT pageids are the same
    work's pages in different language editions — flipping the primary
    between them on alternate upserts would destabilize the display link, so
    the creator's primary is kept (the incoming id still joins the identity
    bag). The same pageid upgrades the legacy bare form to the qualified one.

    A synthetic per-season primary (``{series_id}#s{N}``) keeps its season:
    a series-level or different-season incoming id never replaces it (the
    series-level id lives on the collection's bag instead).
    """
    ex_split = split_season_identity(existing)
    if ex_split is not None:
        in_split = split_season_identity(incoming)
        if in_split is None or in_split[1] != ex_split[1]:
            return existing
        return incoming
    ex_lang, ex_pid = parse_wikipedia_id(existing)
    in_lang, in_pid = parse_wikipedia_id(incoming)
    if ex_pid and in_pid:
        if ex_pid != in_pid:
            return existing
        return incoming if in_lang else existing
    return incoming


async def upsert_episodes(
    db: AsyncSession,
    series: TVSeries,
    episode_list: list[dict],
    *,
    entity_granularity: str = "series",
) -> int:
    """Idempotently upsert Episode rows from a parsed Wikipedia episode_list.

    Keyed by (series_id, season, episode); existing rows get their title /
    air_date refreshed (when the incoming value is non-null), missing rows
    are inserted. Additive only - extra rows (e.g. manually curated or from
    a source no longer listing them) are never deleted this phase. Returns
    the number of episode entries processed.

    Per-season model (作品单季化): an unsplit legacy row (multi-season
    ``seasons`` evidence on the inert columns) still absorbs every season's
    entries; a per-season work only takes its own season's entries from a
    series-level entity, and re-tags a season-granularity entity's entries to
    its ``season_number`` (a Bangumi subject IS one season; its entries may
    carry the resource's parsed marker or a default 1).
    """
    items = [
        e for e in (episode_list or [])
        if e.get("season") is not None and e.get("episode") is not None
    ]
    if not items:
        return 0
    if not is_unsplit_legacy_series(series):
        season = getattr(series, "season_number", None) or 1
        if entity_granularity == "season":
            items = [{**e, "season": season} for e in items]
        else:
            items = [e for e in items if int(e["season"]) == season]
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


# ---------------------------------------------------------------------------
# Per-season work upsert helpers (作品单季化 P3)
# ---------------------------------------------------------------------------


def _identity_granularity(
    raw_source: str | None, canonical_id: str | None
) -> tuple[str, str | None, int | None]:
    """Classify an incoming TV identity by registry granularity.

    Returns ``(granularity, series_level_id, synthetic_season)``:
    a synthetic ``{id}#s{N}`` form is a per-season identity wrapping a
    series-level id; otherwise the canonical registry prefix decides (a
    non-registry source — ``exa``/``llm_search``/``exa_web`` — defaults to
    ``"series"``: open-web pages are series-level in practice).
    """
    if canonical_id:
        split = split_season_identity(canonical_id)
        if split is not None:
            return "season", split[0], split[1]
        if ":" in canonical_id:
            prefix = canonical_id.split(":", 1)[0]
            gran = granularity_of(prefix, "tv")
            if gran is not None:
                # A season-granularity id IS the per-season identity — no
                # series-level id to derive a synthetic form from.
                if gran == "season":
                    return "season", None, None
                return "series", canonical_id, None
    gran = granularity_of(raw_source, "tv")
    if gran == "season":
        return "season", None, None
    return "series", canonical_id, None


def _title_season_from_entity(data: dict) -> int | None:
    """Season marker from the matched entity's own titles, if any.

    Direct suffixes (``第N季`` / ``Season N`` / ``S04``) are trusted as-is.
    Sequel-number forms (``X 3`` / ``無職転生Ⅲ``) are only trusted when the
    entity's own season structure confirms that season exists — a trailing
    number alone is too ambiguous (年份 / 标题本身含数字).
    """
    titles = [
        t for t in (
            data.get("title_cn"),
            data.get("title_en"),
            data.get("original_title"),
            *(data.get("alt_titles") or []),
        ) if t
    ]
    for t in titles:
        n = season_from_title(t)
        if n is not None:
            return n
    from app.services.metadata_episode_reconcile import _trailing_sequel_number

    available = {
        s["season_number"]
        for s in data.get("seasons") or []
        if isinstance(s, dict) and isinstance(s.get("season_number"), int)
    }
    if not available:
        count = data.get("number_of_seasons")
        if isinstance(count, int) and not isinstance(count, bool) and count >= 1:
            available = set(range(1, count + 1))
    if not available:
        return None
    candidates = {_trailing_sequel_number(t) for t in titles} - {None}
    if len(candidates) == 1:
        n = next(iter(candidates))
        if n in available:
            return n
    return None


def _season_entry(data: dict, season: int | None) -> dict | None:
    """The entity's ``seasons`` entry for one season number, if present."""
    if season is None:
        return None
    for s in data.get("seasons") or []:
        if isinstance(s, dict) and s.get("season_number") == season:
            return s
    return None


def _season_episode_subset(data: dict, season: int | None) -> list[dict]:
    """The entity's ``episode_list`` entries belonging to one season."""
    if season is None:
        return []
    return [
        e
        for e in data.get("episode_list") or []
        if isinstance(e, dict) and e.get("season") == season
    ]


def _work_episode_count(data: dict, season: int, granularity: str) -> int | None:
    """Season-scoped episode count for a per-season work.

    Never the series total: season-granularity entities (bangumi …) carry the
    season's own count; series-level entities contribute the season's
    ``seasons`` entry / ``episode_list`` subset; a verified single-season
    entity's total is the season count by definition.
    """
    if granularity == "season":
        return data.get("number_of_episodes")
    entry = _season_entry(data, season)
    if entry and isinstance(entry.get("episode_count"), int):
        return entry["episode_count"]
    subset = _season_episode_subset(data, season)
    if subset:
        return len(subset)
    if verified_season_count(data) == 1:
        return data.get("number_of_episodes")
    return None


def _work_start_date(data: dict, season: int, granularity: str):
    """Season premiere: the parent entry's per-season data, else None."""
    if granularity == "season":
        return _parse_date(data.get("start_date"))
    entry = _season_entry(data, season)
    if entry:
        d = _parse_date(entry.get("air_date"))
        if d:
            return d
    dates = sorted(
        e["air_date"] for e in _season_episode_subset(data, season) if e.get("air_date")
    )
    if dates:
        return _parse_date(dates[0])
    if season == 1 or verified_season_count(data) == 1:
        return _parse_date(data.get("start_date"))
    return None


def _work_end_date(data: dict, season: int, granularity: str):
    """Season finale: per-season evidence only (series end-date is S-last's)."""
    if granularity == "season":
        return _parse_date(data.get("end_date"))
    entry = _season_entry(data, season)
    if entry:
        d = _parse_date(entry.get("end_date"))
        if d:
            return d
    dates = sorted(
        e["air_date"] for e in _season_episode_subset(data, season) if e.get("air_date")
    )
    if dates:
        return _parse_date(dates[-1])
    if verified_season_count(data) == 1:
        return _parse_date(data.get("end_date"))
    return None


async def _collection_members(db: AsyncSession, collection_id: str) -> list[TVSeries]:
    """Season works of a collection, ordered by season number."""
    return list(
        (
            await db.execute(
                select(TVSeries)
                .where(TVSeries.collection_id == collection_id)
                .order_by(TVSeries.season_number, TVSeries.created_at)
            )
        )
        .scalars()
        .all()
    )


async def _find_collection_by_titles(
    db: AsyncSession, titles: list[str]
) -> WorkCollection | None:
    """Two-level title fallback, level 1: base title → collection match.

    Normalized comparison over the collection's titles and aliases (same
    Python-side scan pattern as the franchise-pack get-or-create — the
    collection table is small).
    """
    norms = {n for t in titles if t for n in [normalize_title(t)] if n}
    if not norms:
        return None
    rows = (await db.execute(select(WorkCollection))).scalars().all()
    for coll in rows:
        candidates = [coll.title_cn, coll.title_en, *(coll.aliases or [])]
        if any(normalize_title(c) in norms for c in candidates if c):
            return coll
    return None


def _merge_collection_aliases(collection: WorkCollection, data: dict) -> None:
    """Merge a matched entity's titles (raw + base forms) into collection aliases."""
    if field_manually_edited(collection, "aliases"):
        return
    existing = {
        t for t in [collection.title_cn, collection.title_en, *(collection.aliases or [])] if t
    }
    new_aliases = list(collection.aliases or [])
    for t in (
        data.get("title_cn"),
        data.get("title_en"),
        data.get("original_title"),
        *(data.get("alt_titles") or []),
    ):
        for cand in (t, strip_season_from_title(t)):
            if cand and cand not in existing and cand not in new_aliases:
                new_aliases.append(cand)
                existing.add(cand)
    collection.aliases = new_aliases or None


async def _create_series_collection(db: AsyncSession, data: dict) -> WorkCollection:
    """Create the shell collection for a freshly-matched series-level entity."""
    raw_cn = data.get("title_cn")
    raw_en = data.get("title_en") or data.get("original_title")
    base_cn = strip_season_from_title(raw_cn)
    base_en = strip_season_from_title(raw_en)
    aliases: list[str] = []
    for t in (
        raw_cn,
        raw_en,
        data.get("original_title"),
        base_cn,
        base_en,
        *(data.get("alt_titles") or []),
    ):
        if t and t not in aliases:
            aliases.append(t)
    collection = WorkCollection(
        title_cn=(base_cn or base_en or data.get("original_title") or "未命名系列")[:512],
        title_en=base_en,
        aliases=aliases or None,
        external_id=None,
        external_source="series_group",
        poster_url=data.get("poster_url"),
        description=data.get("description"),
    )
    db.add(collection)
    await db.flush()
    return collection


async def _bag_entity_ids_by_granularity(
    db: AsyncSession,
    *,
    work: TVSeries | None,
    collection: WorkCollection | None,
    data: dict,
    series_level_id: str | None,
) -> None:
    """Bag every id a matched entity carries at its registry granularity.

    Season-granularity ids (bangumi/mal/anilist/douban + synthetic
    ``{id}#s{N}``) go to the season work's bag; series-level ids
    (wikipedia/tmdb/imdb) go to the COLLECTION's bag. The season work
    additionally bags the synthetic ``{series_level_id}#s{season_number}``
    identity so a repeat match of the same series+season hits the bag
    directly. ``work=None`` (season-indeterminate parking) bags only the
    collection-level ids.
    """
    pairs = [(data.get("external_source"), data.get("external_id"))]
    for alt in data.get("alt_external_ids") or []:
        if isinstance(alt, dict):
            pairs.append((alt.get("source"), alt.get("id")))
    for source, eid in pairs:
        canon = canonicalize_external_id(eid, source)
        if not canon:
            continue
        if split_season_identity(canon) is not None:
            gran = "season"
        else:
            prefix = canon.split(":", 1)[0] if ":" in canon else None
            gran = granularity_of(prefix, "tv") or granularity_of(source, "tv") or "series"
        if gran == "season" or collection is None:
            if work is not None:
                await add_external_id(db, "series", work.id, source, eid)
        else:
            await add_external_id(db, "collection", collection.id, source, eid)
    if work is not None and collection is not None and series_level_id:
        prefix = series_level_id.split(":", 1)[0] if ":" in series_level_id else None
        if prefix in REGISTRY_SOURCES:
            await add_external_id(
                db,
                "series",
                work.id,
                prefix,
                make_season_identity(series_level_id, work.season_number),
            )


async def _update_series_from_entity(
    db: AsyncSession,
    series: TVSeries,
    data: dict,
    *,
    raw_source: str | None,
    canonical_id: str | None,
    granularity: str,
) -> None:
    """Apply a matched entity onto an existing TVSeries work.

    Same field semantics as the pre-split upsert (manual-edit protection,
    creator-wins primary, alias merge, poster caching, genre clamp,
    ``apply_is_anime``) with the per-season changes: the inert-orphan
    ``seasons``/``number_of_seasons`` columns are never written, and
    ``number_of_episodes``/``start_date``/``end_date``/``episode_list`` are
    season-scoped for per-season works (legacy unsplit rows keep the old
    series-total semantics until the season-split migration).
    """
    # Migrate legacy/inconsistent identifiers to the canonical form so the
    # next upsert converges even faster. Wikipedia primaries are the
    # exception: different pageids are per-edition pages of the same work,
    # so the creator's primary is kept (incoming ids join the bag).
    if canonical_id and not field_manually_edited(series, "external_id"):
        series.external_id = _merge_primary_external_id(series.external_id, canonical_id)
    if raw_source and raw_source != "llm_search" and not field_manually_edited(series, "external_source"):
        series.external_source = raw_source
    if not series.wikipedia_url and data.get("wikipedia_url"):
        series.wikipedia_url = data["wikipedia_url"]
    if not field_manually_edited(series, "description"):
        series.description = data.get("description") or series.description
    if not field_manually_edited(series, "rating") and data.get("rating") is not None:
        series.rating = data.get("rating")
    if not field_manually_edited(series, "original_title"):
        series.original_title = data.get("original_title") or series.original_title
    if not field_manually_edited(series, "status"):
        series.status = data.get("status") or series.status
    if is_unsplit_legacy_series(series):
        # Legacy series-level row: keep the pre-split series-total semantics.
        episode_count = data.get("number_of_episodes")
        start = _parse_date(data.get("start_date"))
        end = _parse_date(data.get("end_date"))
    else:
        episode_count = _work_episode_count(data, series.season_number, granularity)
        start = _work_start_date(data, series.season_number, granularity)
        end = _work_end_date(data, series.season_number, granularity)
    if episode_count is not None and not field_manually_edited(series, "number_of_episodes"):
        series.number_of_episodes = episode_count
    if not field_manually_edited(series, "start_date"):
        if start:
            series.start_date = start
    if not field_manually_edited(series, "end_date"):
        if end:
            series.end_date = end
    if not field_manually_edited(series, "genre"):
        genres = normalize_genres(data.get("genre"))
        if genres:
            series.genre = genres
    if not field_manually_edited(series, "title_cn"):
        if data.get("title_cn"):
            series.title_cn = series.title_cn or strip_season_from_title(data.get("title_cn"))
    if not field_manually_edited(series, "title_en"):
        if data.get("title_en"):
            series.title_en = series.title_en or strip_season_from_title(data.get("title_en"))

    if not field_manually_edited(series, "aliases"):
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

    if not field_manually_edited(series, "poster_url"):
        remote_poster = data.get("poster_url")
        if remote_poster and not (series.poster_url or "").startswith("/posters/"):
            local_url = await download_and_cache_poster(remote_poster)
            series.poster_url = local_url or remote_poster
    if not field_manually_edited(series, "content_type"):
        series.content_type = "tv"
    apply_is_anime(series, data)
    if data.get("episode_list"):
        await upsert_episodes(db, series, data["episode_list"], entity_granularity=granularity)


async def _create_season_work(
    db: AsyncSession,
    data: dict,
    collection: WorkCollection,
    season: int,
    *,
    raw_source: str | None,
    raw_external_id: str | None,
    canonical_id: str | None,
    granularity: str,
    series_level_id: str | None,
) -> TVSeries:
    """Lazily create the per-season work for one collection member.

    Only the season the match asked for is materialized. Titles keep the
    base (season-stripped) convention with the season-qualified variants in
    ``aliases``; the primary id is the season-granularity canonical id or —
    for series-level entities — the synthetic ``{series_id}#s{N}`` identity
    (when the id has a registry prefix; unregistry-shaped ids stay as-is).
    """
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
    if granularity == "season":
        primary_id = canonical_id or raw_external_id
    elif (
        series_level_id
        and ":" in series_level_id
        and series_level_id.split(":", 1)[0] in REGISTRY_SOURCES
    ):
        primary_id = make_season_identity(series_level_id, season)
    else:
        primary_id = canonical_id or raw_external_id
    work = TVSeries(
        title_cn=title_cn,
        title_en=title_en,
        original_title=data.get("original_title"),
        aliases=aliases or None,
        external_id=primary_id,
        external_source=data.get("external_source", "llm_search"),
        wikipedia_url=data.get("wikipedia_url"),
        description=data.get("description"),
        poster_url=local_url or remote_poster,
        rating=data.get("rating"),
        genre=normalize_genres(data.get("genre")),
        status=data.get("status"),
        number_of_episodes=_work_episode_count(data, season, granularity),
        start_date=_work_start_date(data, season, granularity),
        end_date=_work_end_date(data, season, granularity),
        content_type="tv",
        season_number=season,
        collection_id=collection.id,
    )
    apply_is_anime(work, data)
    db.add(work)
    await db.flush()
    if data.get("episode_list"):
        await upsert_episodes(db, work, data["episode_list"], entity_granularity=granularity)
    await _bag_entity_ids_by_granularity(
        db, work=work, collection=collection, data=data, series_level_id=series_level_id,
    )
    return work


async def _resolve_collection_member(
    db: AsyncSession,
    data: dict,
    collection: WorkCollection,
    season: int | None,
    *,
    raw_source: str | None,
    canonical_id: str | None,
    granularity: str,
    series_level_id: str | None,
) -> TVSeries | None:
    """Select (or lazily create) the collection member for the target season.

    Season unknown: a single-member collection is verifiably single-season →
    its member; a multi-member collection (or a multi-season entity over an
    empty collection) cannot be pinned — the series-level ids are bagged on
    the collection and None is returned so the caller parks the resource on
    the collection for Channel confirmation (挂合集待确认).
    """
    _merge_collection_aliases(collection, data)
    members = await _collection_members(db, collection.id)
    work: TVSeries | None = None
    if season is None:
        if len(members) == 1:
            work = members[0]
        elif len(members) >= 2 or (verified_season_count(data) or 0) > 1:
            await _bag_entity_ids_by_granularity(
                db, work=None, collection=collection, data=data,
                series_level_id=series_level_id,
            )
            return None
        else:
            season = 1
    if work is None:
        work = next((m for m in members if m.season_number == season), None)
    if work is None:
        return await _create_season_work(
            db, data, collection, season,
            raw_source=raw_source,
            raw_external_id=data.get("external_id"),
            canonical_id=canonical_id,
            granularity=granularity,
            series_level_id=series_level_id,
        )
    await _update_series_from_entity(
        db, work, data,
        raw_source=raw_source, canonical_id=canonical_id, granularity=granularity,
    )
    await _bag_entity_ids_by_granularity(
        db, work=work, collection=collection, data=data, series_level_id=series_level_id,
    )
    return work


async def create_or_update_series_from_external(
    db: AsyncSession, data: dict, *, season_hint: int | None = None
) -> TVSeries | None:
    """Upsert a per-season TVSeries work from a matched entity (作品单季化 P3).

    Lookup order:
      1. Per-season identity (synthetic ``{id}#s{N}`` or a season-granularity
         source id: bangumi/mal/anilist/douban) → work identity bag.
      2. Series-level identity (wikipedia/tmdb/imdb) → COLLECTION identity
         bag → member selection by ``(collection_id, season_number)``; a
         missing member is lazily created from the entity's per-season data
         (``seasons``/``episode_list``). Seasons the match did not ask for
         are never materialized.
      3. Legacy compat: ids still bagged on / listed as the primary of a
         pre-split TVSeries row converge on that row unchanged (the
         season-split migration re-homes them).
      4. Two-level title fallback: base title (season-stripped) → collection
         (normalized titles + aliases) → member by season; then a
         season-aware work-title match (a season-known match never lands on
         a different season's work — the pre-split fallback's bug); only
         when both miss, create a fresh collection + season work.

    The target season comes from ``season_hint`` (resource parse context),
    then a synthetic id's ``#s{N}``, then a season marker in the entity
    titles, then verified single-season entity evidence. When the identity /
    title resolves to a collection whose season cannot be determined, the
    ids are bagged on the collection and None is returned — the caller parks
    the resource on the collection for Channel confirmation (挂合集待确认)
    instead of guessing a season.

    On every successful upsert the incoming id(s) are written into the bag
    at their registry granularity; the primary column keeps its creator-wins
    semantics.
    """
    raw_external_id = _qualify_incoming_wikipedia_id(data)
    raw_source = data.get("external_source")
    content_type = data.get("content_type")
    canonical_id = canonicalize_external_id(raw_external_id, raw_source, content_type)
    granularity, series_level_id, synth_season = _identity_granularity(
        raw_source, canonical_id
    )

    season = season_hint or synth_season or _title_season_from_entity(data)
    if season is None and verified_season_count(data) == 1:
        season = 1

    # (1) Per-season identity → work identity bag.
    series: TVSeries | None = None
    if canonical_id and granularity == "season":
        series = await find_work_by_external_id(db, "series", raw_source, raw_external_id)

    # (2) Series-level identity → collection bag → member selection; falling
    # that, the synthetic per-season identity may already be bagged on a work
    # (repeat match of the same series+season).
    if series is None and granularity != "season" and series_level_id:
        collection = await find_work_by_external_id(
            db, "collection", raw_source, raw_external_id
        )
        if collection is not None:
            return await _resolve_collection_member(
                db, data, collection, season,
                raw_source=raw_source, canonical_id=canonical_id,
                granularity=granularity, series_level_id=series_level_id,
            )
        if season is not None:
            series = await find_work_by_external_id(
                db, "series", raw_source, make_season_identity(series_level_id, season)
            )

    # (3) Legacy compat: canonical + raw id shapes against the TVSeries
    # primary column (and ids bagged on pre-split rows). ``llm_search`` is a
    # legacy source label kept for compatibility.
    lookup_ids = {i for i in (canonical_id, series_level_id, raw_external_id) if i}
    lookup_sources = {s for s in (raw_source, "llm_search") if s}

    if series is None and lookup_ids:
        stmt = select(TVSeries).where(_external_id_match(TVSeries.external_id, lookup_ids))
        if lookup_sources:
            stmt = stmt.where(TVSeries.external_source.in_(lookup_sources))
        result = await db.execute(stmt)
        series = result.scalars().first()

    if series is not None:
        await _update_series_from_entity(
            db, series, data,
            raw_source=raw_source, canonical_id=canonical_id, granularity=granularity,
        )
        coll = (
            await db.get(WorkCollection, series.collection_id)
            if series.collection_id
            else None
        )
        await _bag_entity_ids_by_granularity(
            db, work=series, collection=coll, data=data, series_level_id=series_level_id,
        )
        return series

    # (4) Two-level title fallback: base (season-stripped) titles match a
    # collection first, then — season-aware — individual works.
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
    base_titles = list({
        b for c in raw_candidates for b in [strip_season_from_title(c)] if b
    })
    collection = await _find_collection_by_titles(db, [*base_titles, *raw_candidates])
    if collection is not None:
        return await _resolve_collection_member(
            db, data, collection, season,
            raw_source=raw_source, canonical_id=canonical_id,
            granularity=granularity, series_level_id=series_level_id,
        )

    title_candidates = list({
        t for c in raw_candidates for t in (c, strip_season_from_title(c)) if t
    })
    candidates: list[TVSeries] = []
    if title_candidates:
        title_result = await db.execute(
            select(TVSeries)
            .where(
                or_(
                    TVSeries.title_cn.in_(title_candidates),
                    TVSeries.title_en.in_(title_candidates),
                    TVSeries.original_title.in_(title_candidates),
                )
            )
            .order_by(TVSeries.created_at)
        )
        candidates = list(title_result.scalars().all())
    if candidates:
        if season is not None:
            exact = [c for c in candidates if (c.season_number or 1) == season]
            if exact:
                series = exact[0]
                await _update_series_from_entity(
                    db, series, data,
                    raw_source=raw_source, canonical_id=canonical_id,
                    granularity=granularity,
                )
                coll = (
                    await db.get(WorkCollection, series.collection_id)
                    if series.collection_id
                    else None
                )
                await _bag_entity_ids_by_granularity(
                    db, work=series, collection=coll, data=data,
                    series_level_id=series_level_id,
                )
                return series
            # Season-known but no member with that season: create it under
            # the candidates' collection (never collapse onto another
            # season's work — the pre-split fallback's bug).
            coll_id = next((c.collection_id for c in candidates if c.collection_id), None)
            collection = (
                await db.get(WorkCollection, coll_id)
                if coll_id
                else await _create_series_collection(db, data)
            )
            return await _create_season_work(
                db, data, collection, season,
                raw_source=raw_source, raw_external_id=raw_external_id,
                canonical_id=canonical_id, granularity=granularity,
                series_level_id=series_level_id,
            )
        # Season unknown: single candidate (or all-season-1 legacy rows)
        # keeps the old behavior; a multi-season candidate set parks on its
        # shared collection for Channel confirmation instead of guessing.
        seasons_present = {c.season_number or 1 for c in candidates}
        if len(candidates) == 1 or seasons_present == {1}:
            series = candidates[0]
            await _update_series_from_entity(
                db, series, data,
                raw_source=raw_source, canonical_id=canonical_id,
                granularity=granularity,
            )
            coll = (
                await db.get(WorkCollection, series.collection_id)
                if series.collection_id
                else None
            )
            await _bag_entity_ids_by_granularity(
                db, work=series, collection=coll, data=data,
                series_level_id=series_level_id,
            )
            return series
        coll_ids = {c.collection_id for c in candidates if c.collection_id}
        if len(coll_ids) == 1:
            collection = await db.get(WorkCollection, next(iter(coll_ids)))
            if collection is not None:
                _merge_collection_aliases(collection, data)
                await _bag_entity_ids_by_granularity(
                    db, work=None, collection=collection, data=data,
                    series_level_id=series_level_id,
                )
                return None
        return candidates[0]

    # (5) Fresh match: create the shell collection + the season work. A
    # multi-season entity whose season cannot be determined is parked on the
    # new collection (ids bagged) without materializing a guessed season.
    collection = await _create_series_collection(db, data)
    if season is None:
        if (verified_season_count(data) or 0) > 1:
            await _bag_entity_ids_by_granularity(
                db, work=None, collection=collection, data=data,
                series_level_id=series_level_id,
            )
            return None
        season = 1
    return await _create_season_work(
        db, data, collection, season,
        raw_source=raw_source, raw_external_id=raw_external_id,
        canonical_id=canonical_id, granularity=granularity,
        series_level_id=series_level_id,
    )


async def find_collection_for_entity(db: AsyncSession, data: dict) -> WorkCollection | None:
    """Re-resolve the collection a matched entity belongs to (never creates).

    Used when :func:`create_or_update_series_from_external` returned None
    (season indeterminate): the upsert already bagged the series-level ids on
    the collection, so this is a deterministic re-lookup — collection bag
    first, then the two-level title fallback's collection level.
    """
    raw_external_id = data.get("external_id")
    raw_source = data.get("external_source")
    canonical_id = canonicalize_external_id(raw_external_id, raw_source, data.get("content_type"))
    if canonical_id and split_season_identity(canonical_id) is None:
        collection = await find_work_by_external_id(db, "collection", raw_source, raw_external_id)
        if collection is not None:
            return collection
    titles = [
        t for t in (
            data.get("title_cn"),
            data.get("title_en"),
            data.get("original_title"),
            *(data.get("alt_titles") or []),
        ) if t
    ]
    return await _find_collection_by_titles(db, titles)


async def locate_absolute_episode_in_collection(
    db: AsyncSession, collection_id: str, absolute: int | None
) -> tuple[TVSeries, int] | None:
    """Locate an absolute-across-seasons episode along the collection members.

    Replaces the pre-split cross-season arithmetic: members are walked in
    ``season_number`` order accumulating ``number_of_episodes``; the walk
    aborts (None) when a member's count is unknown — cumulative arithmetic is
    impossible without it. The final member gets the reconcile tolerance
    headroom. Returns ``(member_work, per_season_episode)``.
    """
    if absolute is None or absolute < 1:
        return None
    members = [
        m for m in await _collection_members(db, collection_id)
        if isinstance(m.season_number, int) and m.season_number >= 1
    ]
    remaining = absolute
    for index, member in enumerate(members):
        count = member.number_of_episodes
        if not count:
            return None
        if remaining <= count:
            return member, remaining
        if index == len(members) - 1 and remaining <= count + _RECONCILE_TOLERANCE:
            return member, remaining
        remaining -= count
    return None


def pre_reconcile_with_entity(resource: Any, entity: dict | None) -> None:
    """Derive season/episode from a season-less absolute number using the
    matched entity's own per-season counts, BEFORE the upsert — the derived
    season then becomes the upsert's season hint, so an absolute-numbered
    release ("第四季 - 89") still lands on the right per-season work even
    though new works no longer persist a multi-season ``seasons`` column.
    Manual rows are never touched."""
    from app.services.metadata_episode_reconcile import _seasons_map_from

    seasons_map = _seasons_map_from(entity)
    if seasons_map and getattr(resource, "episode_confidence", None) != "manual":
        apply_episode_reconcile(resource, seasons_map)


async def reconcile_linked_series_resource(
    db: AsyncSession,
    resource: Any,
    *,
    series: TVSeries | None = None,
    entity: dict | None = None,
) -> None:
    """Post-link season/episode reconciliation for a TV-linked resource (P3).

    Shared by every link path (known-work short-circuit, mapping/fuzzy
    auto-link, manual link, repository write-back). Order:

      1. history-backed sibling convention (DB evidence);
      2. a season-less resource with an ``absolute_episode`` is located along
         the linked work's collection members (per-season model) and
         re-pointed at the located member;
      3. single-season arithmetic via the work's own episode counts
         (legacy rows: the inert ``seasons`` column);
      4. the verified-season default (:func:`resolve_missing_work` with the
         linked work's identity as evidence).
    """
    if not getattr(resource, "series_id", None):
        return
    if series is None:
        series = await db.get(TVSeries, resource.series_id)
    if series is None:
        return
    from app.services.episode_history import apply_episode_history_reconcile

    resolved = False
    if getattr(resource, "channel_id", None) and getattr(resource, "id", None):
        # History needs channel + id context; attribute-light stand-ins skip it.
        resolved = await apply_episode_history_reconcile(
            db, resource, seasons_map=seasons_map_for_work(series)
        )
    if (
        not resolved
        and getattr(resource, "season", None) is None
        and getattr(resource, "absolute_episode", None) is not None
        and series.collection_id
    ):
        located = await locate_absolute_episode_in_collection(
            db, series.collection_id, resource.absolute_episode
        )
        if located is not None:
            member, episode = located
            resource.series_id = member.id
            resource.season = member.season_number
            resource.episode = episode
            resource.episode_confidence = "reconciled"
            series = member
            resolved = True
    if not resolved:
        seasons_map = seasons_map_for_work(series)
        if seasons_map:
            apply_episode_reconcile(resource, seasons_map)
    resolve_missing_work(
        resource,
        entity if entity is not None else season_evidence_from_series(series),
        work=series,
    )


async def create_or_update_movie_from_external(db: AsyncSession, data: dict) -> Movie:
    """Upsert a Movie by identity-bag, canonical external_id, then exact title.

    See :func:`create_or_update_series_from_external` for the lookup order and
    identity-bag (P3) rationale.
    """
    raw_external_id = _qualify_incoming_wikipedia_id(data)
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
        stmt = select(Movie).where(_external_id_match(Movie.external_id, lookup_ids))
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
        if canonical_id and not field_manually_edited(movie, "external_id"):
            movie.external_id = _merge_primary_external_id(movie.external_id, canonical_id)
        if raw_source and raw_source != "llm_search" and not field_manually_edited(movie, "external_source"):
            movie.external_source = raw_source
        if not movie.wikipedia_url and data.get("wikipedia_url"):
            movie.wikipedia_url = data["wikipedia_url"]
        if not field_manually_edited(movie, "description"):
            movie.description = data.get("description") or movie.description
        if not field_manually_edited(movie, "rating") and data.get("rating") is not None:
            movie.rating = data.get("rating")
        if not field_manually_edited(movie, "original_title"):
            movie.original_title = data.get("original_title") or movie.original_title
        if not field_manually_edited(movie, "status"):
            movie.status = data.get("status") or movie.status
        if not field_manually_edited(movie, "release_date"):
            rd = _parse_date(data.get("release_date"))
            if rd:
                movie.release_date = rd
        if not field_manually_edited(movie, "runtime") and data.get("runtime") is not None:
            movie.runtime = data.get("runtime")
        if not field_manually_edited(movie, "genre"):
            genres = normalize_genres(data.get("genre"))
            if genres:
                movie.genre = genres
        if not field_manually_edited(movie, "title_cn"):
            if data.get("title_cn"):
                movie.title_cn = movie.title_cn or data.get("title_cn")
        if not field_manually_edited(movie, "title_en"):
            if data.get("title_en"):
                movie.title_en = movie.title_en or data.get("title_en")

        if not field_manually_edited(movie, "aliases"):
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

        if not field_manually_edited(movie, "poster_url"):
            remote_poster = data.get("poster_url")
            if remote_poster and not (movie.poster_url or "").startswith("/posters/"):
                local_url = await download_and_cache_poster(remote_poster)
                movie.poster_url = local_url or remote_poster
        if not field_manually_edited(movie, "content_type"):
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
        wikipedia_url=data.get("wikipedia_url"),
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


async def create_or_update_audio_work_from_external(db: AsyncSession, data: dict) -> AudioWork | None:
    """Upsert an AudioWork by canonicalized external_id, then by exact title.

    Mirrors :func:`create_or_update_movie_from_external`. ``data["content_type"]``
    carries the sub-kind (asmr / music / drama_cd / radio / other) and is
    preserved on the entity.

    Returns ``None`` when a *new* row would have no title at all — an AudioWork
    without any of title_cn/title_en/original_title is a useless shell, so
    creation is refused (updates of existing rows are unaffected).
    """
    raw_external_id = _qualify_incoming_wikipedia_id(data)
    raw_source = data.get("external_source")
    content_type = data.get("content_type") or "other"
    canonical_id = canonicalize_external_id(raw_external_id, raw_source, content_type)

    lookup_ids = {i for i in (canonical_id, raw_external_id) if i}
    lookup_sources = {s for s in (raw_source, "llm_search") if s}

    audio: AudioWork | None = None
    if lookup_ids:
        stmt = select(AudioWork).where(_external_id_match(AudioWork.external_id, lookup_ids))
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
            audio.external_id = _merge_primary_external_id(audio.external_id, canonical_id)
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
    title_cn = data.get("title_cn")
    title_en = data.get("title_en") or data.get("original_title")
    if not any((title_cn, data.get("title_en"), data.get("original_title"))):
        logger.warning(
            "[metadata] refusing to create an AudioWork without any title "
            "(external_id=%r, external_source=%r)",
            raw_external_id,
            raw_source,
        )
        return None
    local_url = await download_and_cache_poster(remote_poster)
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
    override_manual_edits: bool = False,
) -> dict:
    """Re-search metadata for an existing TVSeries/Movie and fill missing fields.

    Uses the work's existing titles as the search query against *source* (one
    of the external metadata sources). Only fields that are currently empty on
    the work are filled — existing user/agent values are preserved. Posters are
    downloaded and cached locally like the initial ingestion path.

    Fields the user edited manually (``manually_edited_fields``) are skipped
    unless ``override_manual_edits`` is True — that flag is the explicit
    "覆盖所有人工编辑字段" opt-in offered by the work-detail refresh dialog.

    Returns a summary dict: ``{found, filled, source, message}``.
    """
    is_movie = (content_type or "").lower() == "movie"
    work = await db.get(Movie if is_movie else TVSeries, work_id)
    if not work:
        return {"found": False, "filled": [], "source": source, "message": "work not found"}

    # Season-0 works are specials/SP placeholders: searching by the series
    # title would match the MAIN entry and stuff its series-level data
    # (premiere date, episode count, identity) into the specials work.
    if not is_movie and getattr(work, "season_number", None) == 0:
        return {
            "found": True,
            "filled": [],
            "source": source,
            "message": "season-0 specials work — refresh skipped",
        }

    manual = manually_edited_fields(work)

    search_title = _first_present(work.title_en, work.title_cn, work.original_title)
    if not search_title:
        return {
            "found": True,
            "filled": [],
            "source": source,
            "message": "no title available to search",
        }

    candidates = await search_metadata_via_llm(
        search_title, source,
        season_hint=None if is_movie else work.season_number,
    )
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
        if not override_manual_edits and attr in manual:
            return
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

    if (override_manual_edits or "genre" not in manual) and not work.genre:
        g = normalize_genres(best.get("genre"))
        if g:
            work.genre = g
            filled.append("genre")

    if is_movie:
        fill("release_date", "release_date", _parse_date)
        fill("runtime", "runtime", _safe_int)
    else:
        fill("number_of_episodes", "number_of_episodes", _safe_int)
        # ``number_of_seasons`` is an inert orphan column in the per-season
        # work model — never written. Dates are season-scoped (same semantics
        # as the upsert path): a series-level entity's premiere/finale belongs
        # to season 1 and must not be filled into a later-season work.
        granularity = granularity_of(best.get("external_source"), "tv") or "series"
        season = work.season_number or 1
        start = _work_start_date(best, season, granularity)
        end = _work_end_date(best, season, granularity)
        if (
            (override_manual_edits or "start_date" not in manual)
            and not work.start_date
            and start
        ):
            work.start_date = start
            filled.append("start_date")
        if (
            (override_manual_edits or "end_date" not in manual)
            and not work.end_date
            and end
        ):
            work.end_date = end
            filled.append("end_date")

    # Poster: download + cache, like the initial ingestion path.
    if override_manual_edits or "poster_url" not in manual:
        remote_poster = best.get("poster_url")
        if remote_poster and not (work.poster_url or "").startswith("/posters/"):
            local_url = await download_and_cache_poster(remote_poster)
            work.poster_url = local_url or remote_poster
            filled.append("poster_url")

    identity_rejected = False
    if (override_manual_edits or "external_id" not in manual) and not work.external_id and best.get("external_id"):
        # Identity uniqueness: never grab an id another work already owns
        # (primary column or identity bag) — skip with a warning instead of
        # creating a duplicate (e.g. an S0 special refreshed onto the S1
        # entry's bangumi id).
        new_id = best["external_id"]
        model = Movie if is_movie else TVSeries
        column_taken = (await db.execute(
            select(model.id).where(model.external_id == new_id, model.id != work.id)
        )).first()
        bag_owner = await find_work_by_external_id(
            db, "movie" if is_movie else "series",
            best.get("external_source"), new_id,
        )
        if column_taken or (bag_owner is not None and bag_owner.id != work.id):
            identity_rejected = True
            logger.warning(
                "[refresh] skip external_id %r for work %s: owned by another work",
                new_id, work.id,
            )
        else:
            work.external_id = new_id
            filled.append("external_id")
    if (
        not identity_rejected
        and (override_manual_edits or "external_source" not in manual)
        and not work.external_source
        and best.get("external_source")
    ):
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
# Channel-scoped refresh work selection
# ---------------------------------------------------------------------------

# The fillable-field predicates mirror ``refresh_work_metadata``'s fill list
# (plus the poster/external-id fills): a work matching NONE of them has
# nothing the pipeline could write and is skipped by the periodic
# channel-refresh gate. This is a *selection* filter only — the refresh
# pipeline itself is shared with the manual actions.
_SERIES_HAS_GAP = or_(
    TVSeries.title_cn.is_(None), TVSeries.title_cn == "",
    TVSeries.title_en.is_(None), TVSeries.title_en == "",
    TVSeries.original_title.is_(None), TVSeries.original_title == "",
    TVSeries.description.is_(None), TVSeries.description == "",
    TVSeries.rating.is_(None),
    TVSeries.status.is_(None), TVSeries.status == "",
    TVSeries.genre.is_(None),
    TVSeries.poster_url.is_(None), TVSeries.poster_url == "",
    TVSeries.number_of_episodes.is_(None),
    # number_of_seasons 是作品单季化的惰性孤儿列（per-season 作品恒 NULL），
    # 不再是可填充缺口——留在判定里会让每部季作品永远命中门控。
    TVSeries.start_date.is_(None),
    TVSeries.end_date.is_(None),
    TVSeries.external_id.is_(None), TVSeries.external_id == "",
    TVSeries.external_source.is_(None), TVSeries.external_source == "",
)
_MOVIE_HAS_GAP = or_(
    Movie.title_cn.is_(None), Movie.title_cn == "",
    Movie.title_en.is_(None), Movie.title_en == "",
    Movie.original_title.is_(None), Movie.original_title == "",
    Movie.description.is_(None), Movie.description == "",
    Movie.rating.is_(None),
    Movie.status.is_(None), Movie.status == "",
    Movie.genre.is_(None),
    Movie.poster_url.is_(None), Movie.poster_url == "",
    Movie.release_date.is_(None),
    Movie.runtime.is_(None),
    Movie.external_id.is_(None), Movie.external_id == "",
    Movie.external_source.is_(None), Movie.external_source == "",
)


async def select_channel_works_for_refresh(
    db: AsyncSession, channel_id: str, full_scope: bool = False
) -> list[dict]:
    """Works linked to a channel's resources, for the periodic refresh.

    Returns ``[{id, content_type}, ...]``. With ``full_scope=False`` (the
    default gate) only works carrying at least one fillable empty field are
    returned; ``full_scope=True`` returns every linked work.
    """
    from app.models.file_resource import FileResource

    async def _ids(model, fk_attr, gap_clause):
        stmt = (
            select(getattr(model, "id"))
            .join(FileResource, getattr(FileResource, fk_attr) == model.id)
            .where(FileResource.channel_id == channel_id)
            .distinct()
        )
        if not full_scope:
            stmt = stmt.where(gap_clause)
        return (await db.execute(stmt)).scalars().all()

    series_ids = await _ids(TVSeries, "series_id", _SERIES_HAS_GAP)
    movie_ids = await _ids(Movie, "movie_id", _MOVIE_HAS_GAP)
    return (
        [{"id": sid, "content_type": "tv"} for sid in series_ids]
        + [{"id": mid, "content_type": "movie"} for mid in movie_ids]
    )


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
    # Layer 1: already linked.  Reconciliation still has work to do here:
    # older linked rows may gain a trusted sibling correction later.
    if resource.series_id or resource.movie_id:
        await reconcile_linked_series_resource(db, resource)
        return

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
        await reconcile_linked_series_resource(db, resource)
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
            await reconcile_linked_series_resource(db, resource)
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

        # Respect the channel's configured primary source instead of
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
            resource.metadata_matched_at = utcnow()
        else:
            # Derive the season from a season-less absolute number using the
            # entity's own per-season counts before the upsert (the derived
            # season becomes the season hint).
            pre_reconcile_with_entity(resource, best)
            from app.services.metadata_episode_reconcile import resource_season_hint
            series_entity = await create_or_update_series_from_external(
                db, best, season_hint=resource_season_hint(resource, best)
            )
            if series_entity is not None:
                resource.series_id = series_entity.id
                resource.movie_id = None
                resource.metadata_matched_at = utcnow()
            else:
                # Season indeterminate over a collection: park the resource
                # on the collection for Channel confirmation (挂合集待确认).
                from app.services.metadata_episode_reconcile import (
                    park_resource_on_collection,
                )
                collection = await find_collection_for_entity(db, best)
                if collection is not None:
                    park_resource_on_collection(resource, collection)
                    resource.metadata_matched_at = utcnow()
                else:
                    _record_unmatched_attempt(resource, "not_found")
                    return
        await reconcile_linked_series_resource(db, resource)
        await classify_is_anime_post_link(db, channel, resource)
    except Exception as e:
        logger.warning("[metadata] Failed to link via LLM for %r: %s", search_title[:60], e)
        _record_unmatched_attempt(resource, "transient")


async def manual_search_metadata(
    db: AsyncSession,
    search_title: str,
    content_type: str,
    data_source_type: str | None = None,
    fallback_sources: list[str] | None = None,
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

    results = await search_metadata_via_llm(
        search_title, data_source_type, fallback_sources
    )
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
) -> TVSeries | Movie | None:
    """Manually link a resource to user-selected metadata.

    Creates/updates the entity, sets resource FKs, upserts the
    ChannelRawTitleMapping so future identical titles auto-link.
    Returns None when a tv selection resolves to a collection whose season
    cannot be determined — the resource is parked on the collection for
    Channel confirmation instead of being bound to a guessed season work.
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
        # A movie carries no episode/season question: relinking to a movie
        # settles any stale "ambiguous" flag left over from a previous tv link.
        if getattr(resource, "episode_confidence", None) == "ambiguous":
            resource.episode_confidence = None
    else:
        # Derive the season from a season-less absolute number using the
        # selected entity's own per-season counts before the upsert — the
        # derived season selects/creates the right per-season work.
        pre_reconcile_with_entity(resource, selected_result)
        from app.services.metadata_episode_reconcile import resource_season_hint
        entity = await create_or_update_series_from_external(
            db, selected_result, season_hint=resource_season_hint(resource, selected_result)
        )
        content_type = "tv"
        movie_id = None
        if entity is None:
            # Season indeterminate over a collection: park the resource on
            # the collection for Channel confirmation; no per-work mapping is
            # written (there is no work to point it at).
            from app.services.metadata_episode_reconcile import (
                park_resource_on_collection,
            )
            collection = await find_collection_for_entity(db, selected_result)
            if collection is not None:
                park_resource_on_collection(resource, collection)
            series_id = None
        else:
            resource.series_id = entity.id
            resource.movie_id = None
            series_id = entity.id
            # Manual relink bypasses _apply_to_resource: run the same episode
            # reconciliation + verified season rule (resolve_missing_work)
            # against the freshly-upserted work, so a season-less resource
            # doesn't keep a guessed/empty season.
            await reconcile_linked_series_resource(
                db, resource, series=entity, entity=selected_result
            )

    resource.metadata_matched_at = utcnow()

    # Upsert ChannelRawTitleMapping
    # Use search_title_key so future resources from the same work (different
    # episode/resolution) also auto-link. Skipped when the link parked on a
    # collection without a work (a mapping needs a work FK).
    search_key = (
        normalize_title(extract_search_title(resource))
        if series_id or movie_id
        else None
    )
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
