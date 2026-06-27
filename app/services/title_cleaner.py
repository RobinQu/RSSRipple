"""Title cleaning service for extracting searchable work titles from raw RSS titles.

Two extraction methods are supported:

* **regex** — A user-editable regex pattern (optionally LLM-generated) that
  captures the core title. Stored on the Channel as ``title_extraction_regex``.
* **llm** — No regex; the LLM directly extracts the title from the raw string.
  Results are cached in ``MetadataCache`` with source ``"llm_title"`` so each
  unique title is only sent to the LLM once.
"""

import json
import logging
import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.metadata_cache import MetadataCache
from app.services.feed_analyzer import call_llm

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Method 1: Regex cleanup
# ---------------------------------------------------------------------------

def clean_title_regex(title: str, regex_pattern: str) -> str:
    """Apply a regex to extract the core title from a field-mapping-extracted title.

    The regex should have a capture group (group 1) containing the core title.
    If the regex has no groups, the full match is used.
    If the regex doesn't match, the original title is returned unchanged.

    Args:
        title: The title extracted by field_mapping (may still contain noise
            like season markers, trailing spaces, etc.)
        regex_pattern: A regex string with a capture group for the core title.

    Returns:
        The cleaned title, or the original if the regex doesn't match.
    """
    if not regex_pattern or not regex_pattern.strip():
        return title.strip()
    try:
        match = re.search(regex_pattern, title)
        if match:
            if match.groups():
                return match.group(1).strip()
            return match.group(0).strip()
    except re.error as e:
        logger.warning("Invalid title extraction regex %r: %s", regex_pattern, e)
    return title.strip()


# ---------------------------------------------------------------------------
# Method 2: LLM extraction
# ---------------------------------------------------------------------------

_TITLE_EXTRACTION_SYSTEM_PROMPT = """You are a title extraction assistant for anime/TV/movie RSS feeds.

Given a raw RSS entry title, extract ONLY the core title of the work — the name you would use to search on TMDB/IMDB.

Remove ALL of the following from the title:
- Subtitle group names in brackets: [Lilith-Raws], [LoliHouse], [ANi], etc.
- Season markers: "Season 2", "第二季", "2nd Season", "S2", "II", "Ⅲ"
- Episode numbers: "- 12", "EP12", "#12", "S04E10", "第12话"
- Quality/format tags: [1080p], [WebRip], [HEVC-10bit], [Baha], [WEB-DL]
- Codec/audio info: AAC, FLAC, AVC, x264, etc.
- File extensions: .mkv, .mp4, .avi
- Subtitle type markers: [CHS], [CHT], [简繁内封字幕]
- Container format: [MKV], [MP4]

Keep the title in its original language (Chinese, Japanese, or English).
If both Chinese and English titles are present (separated by " / "), return the first one (usually Chinese).
Do NOT translate the title.
Do NOT add quotes, brackets, or any explanation.
Return ONLY the extracted title text, nothing else."""

_TITLE_EXTRACTION_USER_TEMPLATE = """Extract the core title from this RSS entry title:

{title}

Return ONLY the extracted title, nothing else."""

# Source label used in MetadataCache for LLM-extracted titles
_LLM_TITLE_CACHE_SOURCE = "llm_title"


async def clean_title_llm(title: str, db: AsyncSession) -> str:
    """Extract the core title using the LLM.

    Results are cached in ``MetadataCache`` (source=``"llm_title"``) so each
    unique title is only sent to the LLM once.

    Args:
        title: The raw or field-mapping-extracted title (may contain noise).
        db: Database session for cache access.

    Returns:
        The LLM-extracted clean title, or the original title if the LLM fails.
    """
    title = title.strip()
    if not title:
        return title

    # 1. Check cache
    result = await db.execute(
        select(MetadataCache).where(
            MetadataCache.title == title,
            MetadataCache.source == _LLM_TITLE_CACHE_SOURCE,
        )
    )
    cached = result.scalar_one_or_none()
    if cached:
        clean = cached.metadata_json.get("clean_title", title)
        logger.debug("LLM title cache hit: %r -> %r", title[:40], clean[:40])
        return clean

    # 2. Call LLM
    logger.debug("LLM title cache miss, extracting: %r", title[:40])
    messages = [
        {"role": "system", "content": _TITLE_EXTRACTION_SYSTEM_PROMPT},
        {"role": "user", "content": _TITLE_EXTRACTION_USER_TEMPLATE.format(title=title)},
    ]

    try:
        clean = await call_llm(messages)
        clean = clean.strip()
        # Remove any quotes the LLM might have added
        clean = clean.strip('"\'`')
        # If the LLM returned multi-line, take the first non-empty line
        lines = [l.strip() for l in clean.split("\n") if l.strip()]
        if lines:
            clean = lines[0]
        if not clean:
            clean = title
    except Exception as e:
        logger.error("LLM title extraction failed for %r: %s", title[:40], e)
        clean = title

    # 3. Store in cache
    cache_entry = MetadataCache(
        title=title,
        source=_LLM_TITLE_CACHE_SOURCE,
        content_type="title_cleaning",
        metadata_json={"clean_title": clean},
    )
    db.add(cache_entry)
    await db.commit()

    return clean


# ---------------------------------------------------------------------------
# Regex generation (Method 1: LLM generates initial regex from feed samples)
# ---------------------------------------------------------------------------

_REGEX_GEN_SYSTEM_PROMPT = """You are an RSS title analysis expert. Given sample RSS entry titles, generate a SINGLE regex pattern that extracts the core title (the work's actual name) from each title.

The regex must:
- Have a capture group (group 1) containing the core title
- Strip subtitle group names in brackets [Lilith-Raws], [LoliHouse], etc.
- Strip season markers: "Season 2", "第二季", "2nd Season", "S2", "II"
- Strip episode numbers: "- 12", "EP12", "#12", "S04E10"
- Strip quality/format tags: [1080p], [WebRip], [HEVC-10bit]
- Strip codec/audio/container info: AAC, FLAC, AVC, MKV, etc.
- Work across different title formats in the feed

Output ONLY the regex pattern string. No delimiters (no leading/trailing /), no flags, no explanation, no code fence.

Example output:
^\\]([^\\[]+?)\\s*(?:-|\\[)

The regex will be used with Python's `re.search()` function."""

_REGEX_GEN_USER_TEMPLATE = """Analyze these {count} sample RSS entry titles and generate a regex pattern that extracts the core title from each:

{samples}

Output ONLY the regex pattern, nothing else."""


async def generate_title_regex(entries: list[dict]) -> str | None:
    """Use the LLM to generate a title cleanup regex from sample feed entries.

    Args:
        entries: List of feedparser entry dicts (as returned by
            ``get_raw_entries``). The ``title`` field is used.

    Returns:
        A regex pattern string, or None if the LLM fails.
    """
    titles = [e.get("title", "") for e in entries if e.get("title")]
    if not titles:
        return None

    samples = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(titles[:10]))
    messages = [
        {"role": "system", "content": _REGEX_GEN_SYSTEM_PROMPT},
        {"role": "user", "content": _REGEX_GEN_USER_TEMPLATE.format(count=len(titles[:10]), samples=samples)},
    ]

    try:
        response = await call_llm(messages)
        regex = response.strip()
        # Strip code fences if present
        if regex.startswith("```"):
            regex = regex.split("\n", 1)[-1] if "\n" in regex else regex[3:]
        if regex.endswith("```"):
            regex = regex[:-3]
        regex = regex.strip().strip("`")
        # Validate the regex compiles
        re.compile(regex)
        return regex
    except Exception as e:
        logger.error("LLM regex generation failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# Backfill: batch-extract titles for resources that don't have one yet
# ---------------------------------------------------------------------------

async def backfill_titles(channel, db: AsyncSession) -> int:
    """Extract clean titles for FileResources that don't have ``search_title`` yet.

    Called by the fetch pipeline after new resources are created. Processes
    all resources for the channel that have ``search_title IS NULL``, so it
    also backfills existing resources from previous fetches.

    Uses the channel's ``title_extraction_method`` and ``title_extraction_regex``.
    LLM results are cached in ``MetadataCache`` (source=``"llm_title"``) so
    resources with the same title (e.g. same anime, different episodes) only
    trigger one LLM call.

    Args:
        channel: The Channel ORM instance (provides extraction config).
        db: Database session.

    Returns:
        Number of resources that had their title extracted.
    """
    from app.models.file_resource import FileResource
    from app.services.metadata_service import extract_search_title, apply_title_extraction

    result = await db.execute(
        select(FileResource).where(
            FileResource.channel_id == channel.id,
            FileResource.search_title.is_(None),
        )
    )
    resources = result.scalars().all()

    if not resources:
        return 0

    count = 0
    for resource in resources:
        # 1. Extract base title from parsed fields or raw title
        base_title = extract_search_title(resource)
        if not base_title:
            # Can't extract anything — mark with empty string to avoid retry
            resource.search_title = ""
            count += 1
            continue

        # 2. Apply channel's extraction method (regex or LLM)
        try:
            clean_title = await apply_title_extraction(
                base_title,
                channel.title_extraction_method,
                channel.title_extraction_regex,
                db,
            )
        except Exception as e:
            logger.warning("Title extraction failed for resource %s: %s", resource.id, e)
            clean_title = base_title  # Fallback to base title

        # 3. Store result (even on failure — set to base title to avoid retry)
        resource.search_title = clean_title or base_title
        count += 1

    await db.flush()
    logger.info("Backfilled %d titles for channel %s", count, channel.id)
    return count
