"""Authoritative genre registry — the single source of truth for work genres.

The canonical taxonomy is the TMDB closed genre set (movie 19 + TV 16, union
27 — 8 shared). All metadata paths (TMDB direct, LLM judge/ReAct output, web fallback,
manual PATCH) must land on this closed set: producers emit raw values and
``normalize_genres`` clamps them to canonical English TMDB names at every
exit point (LLM result assembly, metadata write-back, backfill).

Database stores the canonical English TMDB names; Chinese display names live
here for server-side use and in the frontend i18n files (kept in sync by
hand — see ``frontend/src/constants/genres.ts``).

Keep in sync: ``app/schemas/genre.py`` (GenreName Literal) and
``frontend/src/constants/genres.ts``.
"""

from __future__ import annotations

import logging
from typing import Any, TypedDict

logger = logging.getLogger("rssripple.metadata")


class GenreInfo(TypedDict):
    name: str  # canonical English TMDB name (stored in DB)
    tmdb_id: int
    zh_name: str
    applies_to: str  # "movie" | "tv" | "both"


# TMDB closed genre set: movie 19 + TV 16, union 27 (8 shared).
GENRES: list[GenreInfo] = [
    {"name": "Action", "tmdb_id": 28, "zh_name": "动作", "applies_to": "movie"},
    {"name": "Adventure", "tmdb_id": 12, "zh_name": "冒险", "applies_to": "movie"},
    {"name": "Animation", "tmdb_id": 16, "zh_name": "动画", "applies_to": "both"},
    {"name": "Comedy", "tmdb_id": 35, "zh_name": "喜剧", "applies_to": "both"},
    {"name": "Crime", "tmdb_id": 80, "zh_name": "犯罪", "applies_to": "both"},
    {"name": "Documentary", "tmdb_id": 99, "zh_name": "纪录", "applies_to": "both"},
    {"name": "Drama", "tmdb_id": 18, "zh_name": "剧情", "applies_to": "both"},
    {"name": "Family", "tmdb_id": 10751, "zh_name": "家庭", "applies_to": "both"},
    {"name": "Fantasy", "tmdb_id": 14, "zh_name": "奇幻", "applies_to": "movie"},
    {"name": "History", "tmdb_id": 36, "zh_name": "历史", "applies_to": "movie"},
    {"name": "Horror", "tmdb_id": 27, "zh_name": "恐怖", "applies_to": "movie"},
    {"name": "Music", "tmdb_id": 10402, "zh_name": "音乐", "applies_to": "movie"},
    {"name": "Mystery", "tmdb_id": 9648, "zh_name": "悬疑", "applies_to": "both"},
    {"name": "Romance", "tmdb_id": 10749, "zh_name": "爱情", "applies_to": "movie"},
    {"name": "Science Fiction", "tmdb_id": 878, "zh_name": "科幻", "applies_to": "movie"},
    {"name": "TV Movie", "tmdb_id": 10770, "zh_name": "电视电影", "applies_to": "movie"},
    {"name": "Thriller", "tmdb_id": 53, "zh_name": "惊悚", "applies_to": "movie"},
    {"name": "War", "tmdb_id": 10752, "zh_name": "战争", "applies_to": "movie"},
    {"name": "Western", "tmdb_id": 37, "zh_name": "西部", "applies_to": "both"},
    {"name": "Action & Adventure", "tmdb_id": 10759, "zh_name": "动作冒险", "applies_to": "tv"},
    {"name": "Kids", "tmdb_id": 10762, "zh_name": "儿童", "applies_to": "tv"},
    {"name": "News", "tmdb_id": 10763, "zh_name": "新闻", "applies_to": "tv"},
    {"name": "Reality", "tmdb_id": 10764, "zh_name": "真人秀", "applies_to": "tv"},
    {"name": "Sci-Fi & Fantasy", "tmdb_id": 10765, "zh_name": "科幻奇幻", "applies_to": "tv"},
    {"name": "Soap", "tmdb_id": 10766, "zh_name": "肥皂剧", "applies_to": "tv"},
    {"name": "Talk", "tmdb_id": 10767, "zh_name": "脱口秀", "applies_to": "tv"},
    {"name": "War & Politics", "tmdb_id": 10768, "zh_name": "战争政治", "applies_to": "tv"},
]

GENRE_NAMES: list[str] = [g["name"] for g in GENRES]

TMDB_ID_TO_NAME: dict[int, str] = {g["tmdb_id"]: g["name"] for g in GENRES}

_NAME_LOWER_TO_CANONICAL: dict[str, str] = {g["name"].lower(): g["name"] for g in GENRES}

# Common aliases producers (LLM transcription, legacy rows) may emit.
_ALIASES: dict[str, str] = {
    "sci-fi": "Science Fiction",
    "scifi": "Science Fiction",
    "science-fiction": "Science Fiction",
    "sci-fi & fantasy": "Sci-Fi & Fantasy",
    "sci-fi and fantasy": "Sci-Fi & Fantasy",
    "action and adventure": "Action & Adventure",
    "war and politics": "War & Politics",
    "tv-movie": "TV Movie",
    "television movie": "TV Movie",
    "anime": "Animation",
    "animation & anime": "Animation",
}


def _normalize_one(value: Any) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return TMDB_ID_TO_NAME.get(value)
    if isinstance(value, float):
        return TMDB_ID_TO_NAME.get(int(value))
    if not isinstance(value, str):
        return None
    key = value.strip().lower()
    if not key:
        return None
    if key in _NAME_LOWER_TO_CANONICAL:
        return _NAME_LOWER_TO_CANONICAL[key]
    return _ALIASES.get(key)


def normalize_genres(raw: Any) -> list[str]:
    """Clamp producer-supplied genre values to the canonical closed set.

    Accepts ``list[str]`` / ``list[int]`` (TMDB ids) / a single ``str`` /
    ``None``. Unknown values are dropped (logged at debug) — producers may
    emit anything, but only canonical English TMDB names leave this function.
    De-duplicates while preserving order.
    """
    if raw is None:
        return []
    if isinstance(raw, (str, int, float)) and not isinstance(raw, bool):
        items: list[Any] = [raw]
    elif isinstance(raw, (list, tuple, set)):
        items = list(raw)
    else:
        return []
    result: list[str] = []
    for item in items:
        name = _normalize_one(item)
        if name is None:
            if item is not None and str(item).strip():
                logger.debug("normalize_genres: dropping non-canonical value %r", item)
            continue
        if name not in result:
            result.append(name)
    return result


def genre_zh(name: str) -> str | None:
    """Chinese display name for a canonical genre name (None if unknown)."""
    for g in GENRES:
        if g["name"] == name:
            return g["zh_name"]
    return None


def genre_prompt_block() -> str:
    """Prompt-injection text listing the closed genre set.

    Injected into metadata LLM prompts so the model picks genres from the
    canonical TMDB set instead of inventing free-form tags. The instruction
    is deliberately best-effort: when the source lists no explicit genres,
    the model must infer them from the work's synopsis/categories and prefer
    a broader canonical genre over returning an empty list — a matched work
    should essentially never end up genre-less.
    """
    names = ", ".join(f'"{n}"' for n in GENRE_NAMES)
    return (
        "\n## genre\n"
        "Pick 1-3 genres for the matched work from this closed TMDB genre set ONLY:\n"
        f"[{names}]\n"
        "Use the exact English names above. Do NOT invent, translate, or emit "
        "any value outside this list. If the source does not list genres "
        "explicitly, INFER them from the work's synopsis / categories / "
        "description (e.g. a psychological drama synopsis -> \"Drama\"). "
        "Always return at least one best-effort genre whenever any synopsis "
        "is available — prefer a broader genre over an empty list. Return [] "
        "ONLY when no description/categories exist at all.\n"
    )


def genre_inference_system_prompt() -> str:
    """System prompt for the description-based genre fallback call.

    Used when a matched entity still has no genre after clamping (judge/ReAct
    returned nothing and TMDB listed none): one cheap LLM call classifies the
    synopsis into the closed set. Same best-effort rule as genre_prompt_block.
    """
    names = ", ".join(f'"{n}"' for n in GENRE_NAMES)
    return (
        "You classify TV/movie works into TMDB genres. Given a work's title "
        "and synopsis, pick 1-3 genres from this closed set ONLY:\n"
        f"[{names}]\n"
        "Use the exact English names above; never invent values. Infer from "
        "the synopsis and prefer a broader genre over an empty answer. "
        'Output ONLY a JSON array, e.g. ["Drama", "Thriller"].'
    )
