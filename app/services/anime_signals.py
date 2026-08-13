"""Anime detection — deterministic evidence for ``TVSeries.is_anime`` /
``Movie.is_anime``.

``is_anime`` is a tri-state flag on works: True = Japanese-style animation,
False = confirmed live-action, None = not yet determined. It is orthogonal to
``content_type`` (the medium, tv/movie) — anime movies exist — so the medium
routing in ``metadata_repository`` stays untouched.

Signals, strongest first:

1. **Identity source** — bangumi / MyAnimeList / AniList only host anime. A
   work whose primary ``external_source`` (or any ``alt_external_ids`` entry)
   is one of these is anime, full stop.
2. **Wikipedia** — a page carrying a ``{{Infobox animanga/TVAnime}}`` block
   is an anime TV work (see
   ``wikipedia_episode_parser.has_tvanime_infobox``).
3. **TMDB** — genre ids containing Animation (16) AND Japanese language /
   country (``original_language == "ja"`` or ``"JP"`` in ``origin_country``)
   → anime. Genres present WITHOUT Animation → confirmed live-action (False).
   Animation with a non-Japanese language is Western animation — ambiguous,
   yields None.
4. **LLM judge fallback** — the judge/ReAct finalize schema carries an
   optional ``is_anime`` verdict, used only when no deterministic evidence
   exists.

Assignment rule (:func:`apply_is_anime`): True always sticks and is never
downgraded by later weak evidence; False only fills NULL.
"""

from __future__ import annotations

import re
from typing import Any

from app.services.text_normalizer import normalize_title

# Identity sites that host anime only (subset of metadata_source_registry's
# REGISTRY_SOURCES).
ANIME_IDENTITY_SOURCES: frozenset[str] = frozenset({"bangumi", "mal", "anilist"})

TMDB_ANIMATION_GENRE_ID = 16


def is_anime_identity(
    external_source: str | None,
    alt_external_ids: list[str] | None = None,
) -> bool:
    """True when the work carries a bangumi/mal/anilist identity."""
    if (external_source or "").strip().lower() in ANIME_IDENTITY_SOURCES:
        return True
    for token in alt_external_ids or []:
        prefix = str(token).split(":", 1)[0].strip().lower()
        if prefix in ANIME_IDENTITY_SOURCES:
            return True
    return False


def is_anime_from_tmdb(
    genre_ids: list[int] | None,
    original_language: str | None,
    origin_country: list[str] | None,
) -> bool | None:
    """Tri-state verdict from TMDB genre/language/country fields.

    Animation + Japanese → True; genres present without Animation → False
    (confirmed live-action); anything else (no genre data, or non-Japanese
    animation) → None (unknown).
    """
    if not genre_ids:
        return None
    if TMDB_ANIMATION_GENRE_ID not in genre_ids:
        return False
    lang = (original_language or "").strip().lower()
    countries = {str(c).strip().upper() for c in (origin_country or [])}
    if lang == "ja" or "JP" in countries:
        return True
    return None


def apply_is_anime(work: Any, data: dict) -> None:
    """Assign ``work.is_anime`` from a matched_entity/candidate dict.

    Deterministic identity evidence wins outright. After that, True always
    sticks (Wikipedia TVAnime block, TMDB Animation+ja, confirmed LLM
    verdicts); False only fills NULL — an existing True is never downgraded,
    and an existing False is never unset by the LLM.
    """
    if is_anime_identity(data.get("external_source"), data.get("alt_external_ids")):
        work.is_anime = True
        return
    value = data.get("is_anime")
    if value is True:
        work.is_anime = True
    elif value is False and work.is_anime is None:
        work.is_anime = False


# ---------------------------------------------------------------------------
# Bangumi search verification (layer-1 is_anime detection)
# ---------------------------------------------------------------------------

# Title-equality normalization: NFKC + OpenCC t2s + lowercase via
# normalize_title, then strip book-title marks (《》〈〉) and ALL whitespace.
_BANGUMI_STRIP_RE = re.compile(r"[《》〈〉\s]+")


def bangumi_normalize_title(s: str | None) -> str:
    """Aggressive title-equality normalization for Bangumi name matching."""
    return _BANGUMI_STRIP_RE.sub("", normalize_title(s))


def bangumi_verdict(
    work_titles: list[str | None],
    year: int | None,
    subjects: list[dict],
) -> tuple[bool | None, dict | None]:
    """Match Bangumi search results against a work, tri-state verdict.

    A subject counts when its ``name``/``name_cn`` equals (after
    normalization) any of the work's titles/aliases AND the year guard
    passes (work year unknown → pass; otherwise the subject date must exist
    and be within ±1 year). type 2 (anime) → True, type 6 (三次元,
    live-action) → False, other types are ignored. Returns
    ``(verdict, matched_subject)``; ``(None, None)`` when nothing matches.

    Note Bangumi's anime category is broad ACG — it includes some Western
    animation. That is the accepted semantics of this signal.
    """
    norm_titles = {bangumi_normalize_title(t) for t in work_titles} - {""}
    if not norm_titles:
        return None, None
    for subj in subjects:
        names = {
            bangumi_normalize_title(subj.get("name")),
            bangumi_normalize_title(subj.get("name_cn")),
        } - {""}
        if not (names & norm_titles):
            continue
        if year is not None:
            subj_date = str(subj.get("date") or "")
            subj_year = int(subj_date[:4]) if subj_date[:4].isdigit() else None
            if subj_year is None or abs(subj_year - year) > 1:
                continue
        subj_type = subj.get("type")
        if subj_type == 2:
            return True, subj
        if subj_type == 6:
            return False, subj
    return None, None
