"""Cross-season episode reconciliation.

Pure leaf module - no DB, no LLM. Extracted verbatim from metadata_agent.py
(Phase 0 leaf extraction): convert absolute-across-seasons episode numbers
to per-season numbers using the season/episode_count map from TMDB/Exa.
"""
from __future__ import annotations

# Some RSS titles number episodes absolutely across all seasons (S04 - 84,
# where 84 = cumulative episode count across seasons 1-4) rather than
# per-season. We detect this by checking the raw episode against the
# season's episode_count from TMDB/Exa metadata and converting when the
# arithmetic works out. Values outside the tolerance envelope are flagged
# ``ambiguous`` and routed to AgentSuggestion for manual review.

# Extra headroom for still-airing shows where TMDB's episode_count lags a
# few episodes behind the true count.
_RECONCILE_TOLERANCE = 2


def _seasons_map_from(entity: dict | None) -> dict[int, int]:
    """Extract ``{season_number: episode_count}`` from a matched_entity dict.

    Both TMDB (native ``seasons``) and the Exa Agent schema (which mirrors
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
        # Clamp to season_count when tolerance overshoots — TMDB just being
        # behind on episode_count is the common case.
        final_ep = min(candidate, season_count) if candidate > season_count else candidate
        return final_ep, raw_episode, "reconciled"

    return raw_episode, None, "ambiguous"
