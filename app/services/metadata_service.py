"""Metadata matching service for TVSeries and Movie entities.

Matching flow for a FileResource (per AGENTS.md "Metadata 匹配流程"):
1. Already linked (movie_id / series_id set) → return.
2. ChannelRawTitleMapping exact match by (channel_id, raw_title).
3. Local DB match: exact (title_cn/title_en) then fuzzy (ratio >= 70; auto-link at >=85).
4. LLM web-search fallback (channel.metadata_source == "llm").
5. Link FileResource.movie_id or FileResource.series_id.

Poster caching: poster URLs returned by LLM are downloaded to POSTER_CACHE_DIR
using a sha256-based filename, and the DB pointer is updated to /posters/<file>.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from datetime import date, datetime, UTC
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from sqlalchemy import select, or_
from sqlalchemy.ext.asyncio import AsyncSession
from thefuzz import fuzz

import asyncio

from app.config import settings
from app.models.channel_raw_title_mapping import ChannelRawTitleMapping
from app.models.movie import Movie
from app.models.series import TVSeries

logger = logging.getLogger(__name__)

FUZZY_THRESHOLD = 70
AUTO_LINK_THRESHOLD = 85

_LLM_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


# ---------------------------------------------------------------------------
# Title extraction helpers
# ---------------------------------------------------------------------------

_LEADING_BRACKET_RE = re.compile(r"^\[[^\]]*\]\s*")
_EPISODE_TAIL_RE = re.compile(r"\s*-\s*\d+\b.*$")
_SEASON_EPISODE_RE = re.compile(r"\s+S\d+E\d+\b.*$", re.IGNORECASE)
_TRAILING_BRACKET_RE = re.compile(r"\s*\[[^\]]*\]\s*$")


def extract_search_title(resource: Any) -> str:
    """Extract a base searchable title from a FileResource (sync, no LLM).

    Priority:
    1. ``title_cn`` or ``title_en`` (already parsed by field_mapping)
    2. ``parse_title(title_raw)`` — strips subtitle group, episode, quality
    3. Simple regex cleanup of ``title_raw`` as a last resort
    """
    title = resource.title_cn or resource.title_en
    if title and title.strip():
        return title.strip()

    raw = getattr(resource, "title_raw", None) or ""
    if not raw.strip():
        return raw

    try:
        from app.services.title_parser import parse_title
        parsed = parse_title(raw)
        t = parsed.title_cn or parsed.title_en
        if t and t.strip():
            return t.strip()
    except Exception:
        pass

    cleaned = _LEADING_BRACKET_RE.sub("", raw)
    cleaned = _EPISODE_TAIL_RE.sub("", cleaned)
    cleaned = _SEASON_EPISODE_RE.sub("", cleaned)
    cleaned = _TRAILING_BRACKET_RE.sub("", cleaned)
    cleaned = cleaned.strip()
    return cleaned or raw.strip()


async def apply_title_extraction(
    title: str,
    method: str,
    regex_pattern: str | None,
    db: AsyncSession | None = None,
) -> str:
    """Apply the channel's title extraction method to a base title."""
    if not title:
        return title

    if method == "regex" and regex_pattern:
        from app.services.title_cleaner import clean_title_regex
        return clean_title_regex(title, regex_pattern)
    if method == "llm" and db:
        from app.services.title_cleaner import clean_title_llm
        return await clean_title_llm(title, db)

    return title


# ---------------------------------------------------------------------------
# Poster caching
# ---------------------------------------------------------------------------

async def download_and_cache_poster(remote_url: str | None) -> str | None:
    """Download a poster image to the local cache directory.

    Filename is ``{sha256(url)[:16]}.{ext}``. Returns the local URL path
    ``/posters/<filename>`` on success, or None on failure.
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

    parsed = urlparse(remote_url)
    ext = (Path(parsed.path).suffix or ".jpg").lower()
    if ext not in (".jpg", ".jpeg", ".png", ".webp"):
        ext = ".jpg"
    ext = ext.lstrip(".")

    digest = hashlib.sha256(remote_url.encode("utf-8")).hexdigest()[:16]
    filename = f"{digest}.{ext}"
    local_path = cache_dir / filename

    if local_path.exists():
        return f"/posters/{filename}"

    def _download() -> bytes | None:
        try:
            with httpx.Client(timeout=30, follow_redirects=True) as client:
                resp = client.get(remote_url)
                resp.raise_for_status()
                return resp.content
        except Exception as e:
            logger.warning("[poster] download failed %s: %s", remote_url[:80], e)
            return None

    content = await asyncio.to_thread(_download)
    if not content:
        return None
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
    """Find best matching TVSeries in local DB. Returns (entity, ratio)."""
    if not title:
        return None, 0
    # Exact
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

    # Fuzzy
    all_result = await db.execute(select(TVSeries))
    best: TVSeries | None = None
    best_score = 0
    title_l = title.lower()
    for s in all_result.scalars().all():
        candidates = [c for c in [s.title_cn, s.title_en, *(s.aliases or [])] if c]
        score = max((fuzz.ratio(title_l, c.lower()) for c in candidates), default=0)
        if score > best_score:
            best_score = score
            best = s
    if best_score >= FUZZY_THRESHOLD:
        return best, best_score
    return None, 0


async def match_movie_by_title(db: AsyncSession, title: str) -> tuple[Movie | None, int]:
    """Find best matching Movie in local DB. Returns (entity, ratio)."""
    if not title:
        return None, 0
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

    all_result = await db.execute(select(Movie))
    best: Movie | None = None
    best_score = 0
    title_l = title.lower()
    for m in all_result.scalars().all():
        candidates = [c for c in [m.title_cn, m.title_en, *(m.aliases or [])] if c]
        score = max((fuzz.ratio(title_l, c.lower()) for c in candidates), default=0)
        if score > best_score:
            best_score = score
            best = m
    if best_score >= FUZZY_THRESHOLD:
        return best, best_score
    return None, 0


# ---------------------------------------------------------------------------
# LLM web search
# ---------------------------------------------------------------------------

def _get_search_model() -> str:
    return settings.llm_search_model or settings.llm_model


async def search_metadata_via_llm(title: str) -> list[dict]:
    """Search for metadata using the LLM web-search tool.

    Returns a list of candidate dicts (already sorted best-first by the LLM).
    Returns an empty list when LLM is unavailable or the call fails.
    """
    if not settings.llm_api_key:
        return []
    model = _get_search_model()
    if not model:
        return []

    from openai import AsyncOpenAI
    client = AsyncOpenAI(api_key=settings.llm_api_key, base_url=settings.llm_base_url)

    prompt = (
        f'Search the web for this title and return metadata: "{title}"\n\n'
        "Return ONLY a JSON object (no markdown) containing an array under the key "
        '"results" with up to 5 candidate results ranked by relevance. Each result:\n'
        "{\n"
        '  "content_type": "tv" or "movie",\n'
        '  "title_cn": "Chinese title or null",\n'
        '  "title_en": "English title or null",\n'
        '  "original_title": "original-language title or null",\n'
        '  "description": "brief plot summary",\n'
        '  "poster_url": "direct URL to a poster image or null",\n'
        '  "year": 2024,\n'
        '  "rating": 8.2,\n'
        '  "genre": ["Action"],\n'
        '  "status": "Ended" or "Returning Series" or "Released" etc,\n'
        '  "external_id": "stable provider id or null",\n'
        '  "number_of_episodes": null,\n'
        '  "number_of_seasons": null,\n'
        '  "start_date": "YYYY-MM-DD or null",\n'
        '  "end_date": "YYYY-MM-DD or null",\n'
        '  "release_date": "YYYY-MM-DD or null",\n'
        '  "runtime": null\n'
        "}\n"
        "Identify whether the title is a TV series/anime or a movie. "
        "For TV content use content_type=\"tv\" and fill start_date/end_date/episode counts. "
        "For movies use content_type=\"movie\" and fill release_date/runtime."
    )

    try:
        response = await asyncio.wait_for(
            client.responses.create(
                model=model,
                tools=[{"type": "web_search"}],
                input=prompt,
            ),
            timeout=15.0,  # don't hang a batch of 100 entries on one LLM call
        )
        raw = (response.output_text or "").strip()
        m = _LLM_JSON_RE.search(raw)
        if not m:
            logger.warning("[llm_search] No JSON in response for %r", title)
            return []
        data = json.loads(m.group(0))
    except asyncio.TimeoutError:
        logger.warning("[llm_search] Timeout for %r", title)
        return []
    except Exception as e:
        logger.warning("[llm_search] API call failed for %r: %s", title, e)
        return []

    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list):
        # Fallback: single-result object
        if isinstance(data, dict) and (data.get("title_en") or data.get("title_cn") or data.get("original_title")):
            results = [data]
        else:
            return []

    normalized: list[dict] = []
    for r in results:
        if not isinstance(r, dict):
            continue
        ct = r.get("content_type") or "tv"
        if ct not in ("tv", "movie"):
            ct = "tv"
        ext_id = r.get("external_id")
        if not ext_id:
            ext_id = "llm_" + hashlib.md5(
                ((r.get("title_en") or r.get("title_cn") or r.get("original_title") or title).lower()).encode()
            ).hexdigest()[:12]
        normalized.append({
            "content_type": ct,
            "title_cn": r.get("title_cn"),
            "title_en": r.get("title_en"),
            "original_title": r.get("original_title"),
            "description": r.get("description"),
            "poster_url": r.get("poster_url"),
            "year": r.get("year"),
            "rating": r.get("rating"),
            "genre": r.get("genre") or [],
            "status": r.get("status"),
            "external_id": str(ext_id),
            "external_source": "llm_search",
            "number_of_episodes": r.get("number_of_episodes"),
            "number_of_seasons": r.get("number_of_seasons"),
            "start_date": r.get("start_date"),
            "end_date": r.get("end_date"),
            "release_date": r.get("release_date"),
            "runtime": r.get("runtime"),
        })
    return normalized


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


async def create_or_update_series_from_external(db: AsyncSession, data: dict) -> TVSeries:
    """Upsert a TVSeries by (external_id, external_source='llm_search')."""
    result = await db.execute(
        select(TVSeries).where(
            TVSeries.external_id == data.get("external_id"),
            TVSeries.external_source == data.get("external_source", "llm_search"),
        )
    )
    series = result.scalars().first()

    if series:
        series.description = data.get("description") or series.description
        if data.get("rating") is not None:
            series.rating = data.get("rating")
        series.original_title = data.get("original_title") or series.original_title
        series.status = data.get("status") or series.status
        if data.get("number_of_episodes") is not None:
            series.number_of_episodes = data.get("number_of_episodes")
        if data.get("number_of_seasons") is not None:
            series.number_of_seasons = data.get("number_of_seasons")
        sd = _parse_date(data.get("start_date"))
        if sd:
            series.start_date = sd
        ed = _parse_date(data.get("end_date"))
        if ed:
            series.end_date = ed
        if data.get("genre"):
            series.genre = data.get("genre")
        if data.get("title_cn"):
            series.title_cn = series.title_cn or data.get("title_cn")
        if data.get("title_en"):
            series.title_en = series.title_en or data.get("title_en")

        existing_titles = {t for t in [series.title_cn, series.title_en, *(series.aliases or [])] if t}
        new_aliases = list(series.aliases or [])
        for t in (data.get("title_cn"), data.get("title_en"), data.get("original_title")):
            if t and t not in existing_titles and t not in new_aliases:
                new_aliases.append(t)
                existing_titles.add(t)
        series.aliases = new_aliases or None

        remote_poster = data.get("poster_url")
        if remote_poster and not (series.poster_url or "").startswith("/posters/"):
            local_url = await download_and_cache_poster(remote_poster)
            series.poster_url = local_url or remote_poster
        series.content_type = "tv"
        return series

    # Create
    remote_poster = data.get("poster_url")
    local_url = await download_and_cache_poster(remote_poster)
    title_cn = data.get("title_cn")
    title_en = data.get("title_en") or data.get("original_title")
    aliases: list[str] = []
    for t in (title_cn, title_en, data.get("original_title")):
        if t and t not in aliases:
            aliases.append(t)
    series = TVSeries(
        title_cn=title_cn,
        title_en=title_en,
        original_title=data.get("original_title"),
        aliases=aliases or None,
        external_id=data.get("external_id"),
        external_source=data.get("external_source", "llm_search"),
        description=data.get("description"),
        poster_url=local_url or remote_poster,
        rating=data.get("rating"),
        genre=data.get("genre") or [],
        status=data.get("status"),
        number_of_episodes=data.get("number_of_episodes"),
        number_of_seasons=data.get("number_of_seasons"),
        start_date=_parse_date(data.get("start_date")),
        end_date=_parse_date(data.get("end_date")),
        content_type="tv",
    )
    db.add(series)
    await db.flush()
    return series


async def create_or_update_movie_from_external(db: AsyncSession, data: dict) -> Movie:
    """Upsert a Movie by (external_id, external_source='llm_search')."""
    result = await db.execute(
        select(Movie).where(
            Movie.external_id == data.get("external_id"),
            Movie.external_source == data.get("external_source", "llm_search"),
        )
    )
    movie = result.scalars().first()

    if movie:
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
        if data.get("genre"):
            movie.genre = data.get("genre")
        if data.get("title_cn"):
            movie.title_cn = movie.title_cn or data.get("title_cn")
        if data.get("title_en"):
            movie.title_en = movie.title_en or data.get("title_en")

        existing_titles = {t for t in [movie.title_cn, movie.title_en, *(movie.aliases or [])] if t}
        new_aliases = list(movie.aliases or [])
        for t in (data.get("title_cn"), data.get("title_en"), data.get("original_title")):
            if t and t not in existing_titles and t not in new_aliases:
                new_aliases.append(t)
                existing_titles.add(t)
        movie.aliases = new_aliases or None

        remote_poster = data.get("poster_url")
        if remote_poster and not (movie.poster_url or "").startswith("/posters/"):
            local_url = await download_and_cache_poster(remote_poster)
            movie.poster_url = local_url or remote_poster
        movie.content_type = "movie"
        return movie

    remote_poster = data.get("poster_url")
    local_url = await download_and_cache_poster(remote_poster)
    title_cn = data.get("title_cn")
    title_en = data.get("title_en") or data.get("original_title")
    aliases: list[str] = []
    for t in (title_cn, title_en, data.get("original_title")):
        if t and t not in aliases:
            aliases.append(t)
    movie = Movie(
        title_cn=title_cn,
        title_en=title_en,
        original_title=data.get("original_title"),
        aliases=aliases or None,
        external_id=data.get("external_id"),
        external_source=data.get("external_source", "llm_search"),
        description=data.get("description"),
        poster_url=local_url or remote_poster,
        rating=data.get("rating"),
        genre=data.get("genre") or [],
        status=data.get("status"),
        release_date=_parse_date(data.get("release_date")),
        runtime=data.get("runtime"),
        content_type="movie",
    )
    db.add(movie)
    await db.flush()
    return movie


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

    # Layer 2: ChannelRawTitleMapping
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
        resource.metadata_matched_at = datetime.now(UTC)
        return

    # Layer 3: local match
    search_title = resource.search_title or extract_search_title(resource)
    if not search_title:
        return

    series, s_ratio = await match_series_by_title(db, search_title)
    movie, m_ratio = await match_movie_by_title(db, search_title)

    # Auto-link only at >=85 ratio
    if series and s_ratio >= AUTO_LINK_THRESHOLD and (movie is None or s_ratio >= m_ratio):
        resource.series_id = series.id
        resource.metadata_matched_at = datetime.now(UTC)
        if not series.poster_url or not (series.poster_url or "").startswith("/posters/"):
            pass  # poster already handled if set
        return
    if movie and m_ratio >= AUTO_LINK_THRESHOLD and (series is None or m_ratio > s_ratio):
        resource.movie_id = movie.id
        resource.metadata_matched_at = datetime.now(UTC)
        return

    # NOTE: 70-84 matches are skipped (too ambiguous) and fall through to LLM layer.

    # Layer 4: LLM web search
    if channel.metadata_source != "llm":
        return

    try:
        results = await search_metadata_via_llm(search_title)
    except Exception as e:
        logger.warning("[metadata] LLM search failed for %r: %s", search_title[:60], e)
        return

    if not results:
        return
    best = results[0]
    try:
        if best.get("content_type") == "movie":
            movie_entity = await create_or_update_movie_from_external(db, best)
            resource.movie_id = movie_entity.id
            resource.series_id = None
        else:
            series_entity = await create_or_update_series_from_external(db, best)
            resource.series_id = series_entity.id
            resource.movie_id = None
        resource.metadata_matched_at = datetime.now(UTC)
    except Exception as e:
        logger.warning("[metadata] Failed to link via LLM for %r: %s", search_title[:60], e)


async def manual_search_metadata(
    db: AsyncSession,
    search_title: str,
    content_type: str,
) -> list[dict]:
    """Run LLM search for the manual-search endpoint. No persistence."""
    results = await search_metadata_via_llm(search_title)
    if content_type in ("tv", "movie"):
        # Prefer content type but don't strictly filter — return all candidates
        preferred = [r for r in results if r.get("content_type") == content_type]
        if preferred:
            return preferred
    return results


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
    else:
        entity = await create_or_update_series_from_external(db, selected_result)
        resource.series_id = entity.id
        resource.movie_id = None
        series_id = entity.id
        movie_id = None
        content_type = "tv"

    resource.metadata_matched_at = datetime.now(UTC)

    # Upsert ChannelRawTitleMapping
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
            content_type=content_type,
            series_id=series_id,
            movie_id=movie_id,
        )
        db.add(mapping)

    await db.flush()
    return entity
