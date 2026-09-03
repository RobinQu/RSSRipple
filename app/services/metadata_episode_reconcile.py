"""Cross-season episode reconciliation.

Pure leaf module - no DB, no LLM. Extracted verbatim from metadata_agent.py
(Phase 0 leaf extraction): convert absolute-across-seasons episode numbers
to per-season numbers using the season/episode_count map from TMDB or the
primary source.
"""
from __future__ import annotations

import re

# Some RSS titles number episodes absolutely across all seasons (S04 - 84,
# where 84 = cumulative episode count across seasons 1-4) rather than
# per-season. We detect this by checking the raw episode against the
# season's episode_count from TMDB/web-fallback metadata and converting when the
# arithmetic works out. Values outside the tolerance envelope are flagged
# ``ambiguous`` and routed to Channel resource confirmation.

# Extra headroom for still-airing shows where TMDB's episode_count lags a
# few episodes behind the true count.
_RECONCILE_TOLERANCE = 2

_TRAILING_ARABIC_SEQUEL_RE = re.compile(
    r"(?:\s+|(?<=[\u3400-\u9fff]))([2-9]|[1-9]\d)$"
)
_TRAILING_ROMAN_SEQUEL_RE = re.compile(
    r"(?:\s+|(?<=[\u3400-\u9fff]))([IVX]{2,5})$", re.IGNORECASE
)
_ROMAN_VALUES = {"I": 1, "V": 5, "X": 10}


def _roman_to_int(value: str) -> int | None:
    total = 0
    previous = 0
    for char in reversed(value.upper()):
        current = _ROMAN_VALUES.get(char)
        if current is None:
            return None
        total += -current if current < previous else current
        previous = max(previous, current)
    numerals = ((10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I"))
    remaining = total
    canonical = ""
    for number, numeral in numerals:
        while remaining >= number:
            canonical += numeral
            remaining -= number
    return total if 2 <= total <= 20 and canonical == value.upper() else None


def _trailing_sequel_number(title: str | None) -> int | None:
    if not title:
        return None
    text = title.strip()
    match = _TRAILING_ARABIC_SEQUEL_RE.search(text)
    if match:
        return int(match.group(1))
    match = _TRAILING_ROMAN_SEQUEL_RE.search(text)
    return _roman_to_int(match.group(1)) if match else None


def infer_verified_title_season(resource, entity: dict | None) -> int | None:
    """Infer a sequel suffix only when bilingual titles and metadata agree."""
    if not isinstance(entity, dict):
        return None
    titles = {
        str(value).strip().casefold(): str(value).strip()
        for value in (
            getattr(resource, "title_cn", None),
            getattr(resource, "title_en", None),
        )
        if value and str(value).strip()
    }
    suffixes = [_trailing_sequel_number(title) for title in titles.values()]
    if len(suffixes) < 2 or suffixes[0] is None or len(set(suffixes)) != 1:
        return None
    candidate = suffixes[0]
    available = {
        season.get("season_number")
        for season in entity.get("seasons") or []
        if isinstance(season, dict)
        and isinstance(season.get("season_number"), int)
        and season["season_number"] >= 1
    }
    if candidate in available:
        return candidate
    count = entity.get("number_of_seasons")
    if isinstance(count, int) and not isinstance(count, bool) and candidate <= count:
        return candidate
    return None


def _seasons_map_from(entity: dict | None) -> dict[int, int]:
    """Extract ``{season_number: episode_count}`` from a matched_entity dict.

    Both TMDB (native ``seasons``) and the judge schema (which mirrors
    it) return a list of season dicts. Season 0 = specials and is ignored.
    Returns an empty dict when there's no usable data.
    """
    if not isinstance(entity, dict):
        return {}
    seasons = entity.get("seasons")
    if not isinstance(seasons, list):
        return {}
    out: dict[int, int] = {}
    for s in seasons:
        if not isinstance(s, dict):
            continue
        num = s.get("season_number")
        cnt = s.get("episode_count")
        if not isinstance(num, int) or not isinstance(cnt, int):
            continue
        if num < 1 or cnt < 1:
            continue
        out[num] = cnt
    return out


def reconcile_episode(
    *,
    raw_episode: int,
    raw_season: int,
    seasons_map: dict[int, int],
) -> tuple[int, int | None, str] | None:
    """Decide whether ``raw_episode`` is per-season or absolute-across-seasons.

    Returns ``(episode, absolute_episode, confidence)`` where ``episode`` is
    the per-season number to store on the resource, ``absolute_episode`` is
    the audit value (or None when the raw was already per-season), and
    ``confidence`` is one of ``"raw" | "reconciled" | "ambiguous"``.

    Returns ``None`` when there's no basis to make a call — caller keeps
    the raw episode and (optionally) marks the resource ``"raw"``.

    Algorithm:
      * No entry for ``raw_season`` in ``seasons_map`` → return None. We
        can't tell.
      * ``raw_episode ≤ season_count + tolerance`` → it looks per-season;
        keep as-is (``confidence="raw"``).
      * Otherwise try converting: subtract the episode counts of prior
        seasons. If the candidate lands within ``[1, season_count]`` we
        accept the conversion (``confidence="reconciled"``). Otherwise
        return ``confidence="ambiguous"`` so the caller can route the
        resource to AgentSuggestion instead of dispatching.
    """
    season_count = seasons_map.get(raw_season)
    if season_count is None or season_count <= 0:
        return None

    # Case A — the raw number already looks like a per-season episode.
    if raw_episode <= season_count + _RECONCILE_TOLERANCE:
        return raw_episode, None, "raw"

    # Case B — try treating raw as absolute.
    prev_total = sum(
        cnt for s, cnt in seasons_map.items() if s < raw_season and cnt > 0
    )
    if prev_total <= 0:
        # Season 1 with a raw > season_count is just a strange release; leave
        # it ambiguous.
        return raw_episode, None, "ambiguous"

    candidate = raw_episode - prev_total
    if 1 <= candidate <= season_count + _RECONCILE_TOLERANCE:
        # Keep the inferred value inside the tolerance headroom.  Clamping it
        # to the stale metadata count aliases a newly-aired episode onto the
        # previous one (for example E8 becoming E7).
        return candidate, raw_episode, "reconciled"

    return raw_episode, None, "ambiguous"


def locate_absolute_episode(
    absolute: int, seasons_map: dict[int, int]
) -> tuple[int, int] | None:
    """Locate an absolute-across-seasons episode number as ``(season, episode)``.

    Walks seasons in ascending order subtracting each season's
    ``episode_count``; the season the remainder lands in is the answer. The
    final season gets ``_RECONCILE_TOLERANCE`` headroom, preserving the
    inferred episode number when the metadata source lags a few episodes
    behind. Returns ``None`` when the number overshoots the
    total (+ tolerance) or the map is empty.
    """
    if absolute < 1 or not seasons_map:
        return None
    remaining = absolute
    ordered = sorted((s, c) for s, c in seasons_map.items() if c > 0)
    for index, (season, count) in enumerate(ordered):
        if remaining <= count:
            return season, remaining
        if index == len(ordered) - 1 and remaining <= count + _RECONCILE_TOLERANCE:
            return season, remaining
        remaining -= count
    return None


def verified_season_count(entity: dict | None) -> int | None:
    """Season-count evidence from a matched_entity-style dict.

    Prefers an explicit ``number_of_seasons`` int; falls back to counting the
    ``seasons`` list excluding season 0 (specials). A source may instead mark
    the current matched entry ``single_season_entry=True`` (Bangumi subject
    semantics); this is match-scoped evidence and is not persisted as the
    work's total season count. Returns ``None`` when no source is usable.
    """
    if not isinstance(entity, dict):
        return None
    n = entity.get("number_of_seasons")
    if isinstance(n, int) and not isinstance(n, bool) and n >= 1:
        return n
    seasons = entity.get("seasons")
    if isinstance(seasons, list):
        count = sum(
            1
            for s in seasons
            if isinstance(s, dict)
            and isinstance(s.get("season_number"), int)
            and s["season_number"] >= 1
        )
        if count >= 1:
            return count
    if entity.get("single_season_entry") is True:
        return 1
    return None


def season_evidence_from_series(series) -> dict:
    """Build reusable missing-season evidence from a persisted TV work.

    A Bangumi TVSeries identity represents one subject, and one subject is one
    season. Preserve that entry-level meaning on already-linked/known-work
    paths without claiming ``TVSeries.number_of_seasons == 1``.
    """
    external_source = str(getattr(series, "external_source", "") or "").lower()
    external_id = str(getattr(series, "external_id", "") or "").lower()
    return {
        "number_of_seasons": getattr(series, "number_of_seasons", None),
        "seasons": getattr(series, "seasons", None),
        "single_season_entry": (
            external_source == "bangumi" or external_id.startswith("bangumi:")
        ),
    }


def apply_episode_reconcile(resource, seasons_map: dict[int, int]) -> bool:
    """Apply :func:`reconcile_episode` to a resource in place.

    Shared by every link path (metadata-agent apply, known-work
    short-circuit, mapping/fuzzy auto-link) so a resource that skips the
    agent still gets its absolute-across-seasons episode number converted
    once the linked series' per-season counts are known.

    Skips (returns False) when there is nothing to do: batch resources,
    resources with neither ``episode`` nor ``absolute_episode``, manually
    vetted values (``manual`` confidence), or an already-``reconciled``
    resource whose season is known.

    When ``season`` is missing but ``absolute_episode`` is known (e.g. a
    ``NN(MM)`` double-labelled release), the season+episode are derived from
    the absolute number via :func:`locate_absolute_episode`; numbers beyond
    the total (+ tolerance) are flagged ``ambiguous``. When there is no basis
    to make a call (empty map / unknown season) the resource is marked
    ``"raw"`` only if it carries no confidence tag yet.
    """
    confidence = getattr(resource, "episode_confidence", None)
    if (
        getattr(resource, "is_batch", False)
        or confidence == "manual"
        or (resource.episode is None and getattr(resource, "absolute_episode", None) is None)
    ):
        return False

    if resource.season is None:
        # Season-less path: only an absolute number can locate the season.
        # An already-"reconciled" value (NN(MM) double-label) is exactly the
        # case that needs its season derived — nothing else wrote it.
        absolute = getattr(resource, "absolute_episode", None)
        if absolute is None or not seasons_map:
            if confidence is None:
                resource.episode_confidence = "raw"
            return False
        located = locate_absolute_episode(absolute, seasons_map)
        if located is None:
            resource.episode_confidence = "ambiguous"
            return True
        resource.season, resource.episode = located
        resource.episode_confidence = "reconciled"
        return True

    # Consistency cross-check: when the title gave BOTH a season marker and
    # an absolute-across-seasons number, the two must agree. If the absolute
    # arithmetic locates a DIFFERENT (season, episode) than the resource
    # currently holds, never silently trust one side — flag the resource
    # ambiguous for manual review. ``None`` from locate (no usable map, or
    # the number overshoots the total) is no evidence either way.
    absolute = getattr(resource, "absolute_episode", None)
    if absolute is not None:
        located = locate_absolute_episode(absolute, seasons_map)
        if located is not None and located != (resource.season, resource.episode):
            resource.episode_confidence = "ambiguous"
            return True

    if confidence == "reconciled":
        return False
    if resource.episode is None:
        # Season known but no episode number — nothing to reconcile.
        if confidence is None:
            resource.episode_confidence = "raw"
        return False
    result = reconcile_episode(
        raw_episode=resource.episode,
        raw_season=resource.season,
        seasons_map=seasons_map,
    )
    if result is None:
        if getattr(resource, "episode_confidence", None) is None:
            resource.episode_confidence = "raw"
        return False
    episode, abs_ep, confidence = result
    resource.episode = episode
    if abs_ep is not None:
        resource.absolute_episode = abs_ep
    resource.episode_confidence = confidence
    return True


def seasons_map_from_list(seasons: list | None) -> dict[int, int]:
    """Build the reconcile map from the stored ``TVSeries.seasons`` column."""
    return _seasons_map_from({"seasons": seasons})


def resolve_missing_season(resource, entity: dict | None) -> str | None:
    """Verified season default / season-uncertain marking (season never guessed).

    Shared by every link path that bypasses ``_apply_to_resource`` — the
    metadata-agent known-work short-circuit, ``fetch_and_link_metadata``'s
    agent-free ``_reconcile_with_series``, ``manual_link_metadata`` — and by
    the season backfill script. Runs AFTER :func:`apply_episode_reconcile`:
    reconcile may legitimately derive a season from ``absolute_episode``, so
    only a resource whose ``season`` is STILL None is handled here. Exactly
    one verified season (:func:`verified_season_count` on ``entity`` — a
    matched_entity-style dict with ``number_of_seasons``/``seasons``) →
    ``season = 1``; multi-season or unknown evidence →
    ``episode_confidence = "ambiguous"`` (季号不确定, routed to a human
    Channel resource confirmation downstream).

    Batch resources (合集) only take the verified single-season default —
    ``batch_scope`` None/``"season"`` with exactly one verified season →
    ``season = 1`` — and are NEVER marked ``ambiguous``: per-episode
    confidence has no meaning for a pack, whose uncertainty is gated by the
    batch-coverage confirmation instead. Multi-season / franchise / movie
    packs (``batch_scope`` in ``"multi_season"``/``"franchise"``/``"movies"``)
    are a full no-op.

    No-op for resources whose season is already known and ``manual`` rows
    (user-vetted). Returns ``"season-defaulted"`` / ``"marked-ambiguous"``
    when it changed the resource, else ``None``.
    """
    if (
        getattr(resource, "season", None) is not None
        or getattr(resource, "episode_confidence", None) == "manual"
    ):
        return None
    title_season = infer_verified_title_season(resource, entity)
    if title_season is not None:
        resource.season = title_season
        if not getattr(resource, "is_batch", False):
            resource.episode_confidence = "raw"
        return "season-title-inferred"
    if getattr(resource, "is_batch", False):
        # 合集：仅取 verified 单季默认值；多季/无证据直接不动，绝不标
        # ambiguous（合集待确认走 batch coverage 门禁）。
        if getattr(resource, "batch_scope", None) in (
            "multi_season", "franchise", "movies",
        ):
            return None
        if verified_season_count(entity) == 1:
            resource.season = 1
            return "season-defaulted"
        return None
    if verified_season_count(entity) == 1:
        resource.season = 1
        return "season-defaulted"
    resource.episode_confidence = "ambiguous"
    return "marked-ambiguous"
