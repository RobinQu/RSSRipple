"""S1 work-level short-circuit: a normalized-title index over TVSeries/Movie.

A new episode/release of an already-identified work links directly without an
LLM call. Extracted from ``metadata_agent`` (Phase 4) into a ``WorkTitleIndex``
class that owns the index state + TTL; the agent holds one instance and
delegates ``_find_known_work`` to it.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any, ClassVar

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.resource_parser import strip_season_from_title

# Normalization for the work-level short-circuit (S1). Collapses a title to
# its word characters (letters incl. CJK, digits) lowercased, dropping
# whitespace/punctuation/width variants so "名探偵プリキュア！" and
# "名探偵プリキュア" collide. Goes through ``normalize_title`` (NFKC + OpenCC
# t2s) first so Traditional and Simplified Chinese forms of the same work
# also collide - e.g. a Simplified RSS title matches a Traditional-cased
# TVSeries row. Intentionally exact-after-normalization only - no fuzzy
# matching - so a short-circuit never links to the wrong work.
_NORMALIZE_RE = re.compile(r"[^\w]+", flags=re.UNICODE)


def _normalize_title(s: str | None) -> str:
    if not s:
        return ""
    from app.services.text_normalizer import normalize_title
    return _NORMALIZE_RE.sub("", normalize_title(s))


class WorkTitleIndex:
    """Normalized-title -> (work_type, work_id) index over TVSeries/Movie
    rows, refreshed on a TTL. Lets a new episode/release of an
    already-identified work link directly without an LLM call. Ambiguous
    titles (mapping to >1 work) are skipped so the short-circuit never guesses."""

    _TITLE_INDEX_TTL: ClassVar[float] = 60.0

    def __init__(self) -> None:
        # Work-level short-circuit index state (S1). Built lazily from the DB
        # on first use and refreshed after _TITLE_INDEX_TTL. ``_ambiguous``
        # collects normalized titles that map to >1 distinct work so the
        # short-circuit skips them (no guessing).
        self._title_index: dict[str, tuple[str, str]] | None = None
        self._title_index_ambiguous: set[str] = set()
        self._title_index_at: float = 0.0
        self._title_index_lock = asyncio.Lock()

    async def _ensure_title_index(self, db: AsyncSession) -> dict[str, tuple[str, str]]:
        """Build (or reuse) the normalized-title -> (work_type, work_id) index.

        Loaded from all TVSeries + Movie rows and cached on the agent instance
        for ``_TITLE_INDEX_TTL``. Titles that normalize to the same key but map
        to different works are recorded as ambiguous and excluded.
        """
        if (
            self._title_index is not None
            and (time.monotonic() - self._title_index_at) < self._TITLE_INDEX_TTL
        ):
            return self._title_index
        async with self._title_index_lock:
            # Double-check after acquiring - another task may have rebuilt it.
            if (
                self._title_index is not None
                and (time.monotonic() - self._title_index_at) < self._TITLE_INDEX_TTL
            ):
                return self._title_index
            from sqlalchemy import select

            from app.models.movie import Movie
            from app.models.series import TVSeries

            index: dict[str, tuple[str, str]] = {}
            ambiguous: set[str] = set()

            def _add(key: str | None, work_type: str, work_id: str) -> None:
                # Strip season suffix first so a season-suffixed alias ("X 第四季")
                # matches a base-title resource ("X"), and vice versa.
                n = _normalize_title(strip_season_from_title(key))
                if not n:
                    return
                existing = index.get(n)
                if existing is None:
                    index[n] = (work_type, work_id)
                elif existing != (work_type, work_id):
                    ambiguous.add(n)

            series_rows = (
                await db.execute(
                    select(
                        TVSeries.id,
                        TVSeries.title_cn,
                        TVSeries.title_en,
                        TVSeries.original_title,
                        TVSeries.canonical_name,
                        TVSeries.aliases,
                    )
                )
            ).all()
            for r in series_rows:
                for k in (r.title_cn, r.title_en, r.original_title, r.canonical_name):
                    _add(k, "tv", r.id)
                for alias in (r.aliases or []):
                    _add(alias, "tv", r.id)

            movie_rows = (
                await db.execute(
                    select(
                        Movie.id,
                        Movie.title_cn,
                        Movie.title_en,
                        Movie.original_title,
                        Movie.canonical_name,
                        Movie.aliases,
                    )
                )
            ).all()
            for r in movie_rows:
                for k in (r.title_cn, r.title_en, r.original_title, r.canonical_name):
                    _add(k, "movie", r.id)
                for alias in (r.aliases or []):
                    _add(alias, "movie", r.id)

            self._title_index = index
            self._title_index_ambiguous = ambiguous
            self._title_index_at = time.monotonic()
            return index

    async def find(
        self, resource: Any, db: AsyncSession
    ) -> tuple[str, str] | None:
        """Return (work_type, work_id) if the resource's pre-parsed title
        exactly (after normalization) matches one known TVSeries/Movie, else
        None. Ambiguous titles (mapping to >1 work) return None so the agent
        runs instead of guessing.
        """
        index = await self._ensure_title_index(db)
        for key in (
            getattr(resource, "title_cn", None),
            getattr(resource, "title_en", None),
            getattr(resource, "search_title", None),
        ):
            # Strip season from the resource title too, so "X 第四季" matches a
            # base-titled series "X" (the index keys are also season-stripped).
            n = _normalize_title(strip_season_from_title(key))
            if n and n not in self._title_index_ambiguous and n in index:
                return index[n]
        return None
