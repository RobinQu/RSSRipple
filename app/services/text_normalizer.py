"""Text normalization and similarity scoring for CJK-aware title matching.

Replaces ``thefuzz`` with a deterministic pipeline:

1. **NFKC normalization** — half/full-width unification, compatibility decomposition.
2. **OpenCC ``t2s``** — Traditional Chinese → Simplified Chinese conversion.
3. **Lowercase + whitespace collapse**.

Similarity is computed via **bigram Dice coefficient** (0–100), which works
well for CJK because every character carries meaning and 2-gram overlap is a
strong signal of title relatedness without requiring a word segmenter.
"""

from __future__ import annotations

import logging
import re
import unicodedata

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# OpenCC — lazy singleton (import-time failure should not crash the module)
# ---------------------------------------------------------------------------

_cc: "OpenCC | None" = None  # type: ignore[valid-type]
_cc_loaded = False


def _get_cc() -> "OpenCC | None":  # type: ignore[valid-type]
    global _cc, _cc_loaded
    if not _cc_loaded:
        _cc_loaded = True
        try:
            from opencc import OpenCC
            _cc = OpenCC("t2s")
        except Exception as e:
            logger.warning("[text_normalizer] OpenCC unavailable: %s", e)
            _cc = None
    return _cc


# ---------------------------------------------------------------------------
# Decorative-character stripping (for raw RSS titles)
# ---------------------------------------------------------------------------

_BRACKET_PAIRS = [
    ("[", "]"), ("【", "】"), ("(", ")"), ("（", "）"),
    ("<", ">"), ("〈", "〉"), ("「", "」"), ("『", "』"),
]
_BRACKET_CONTENT_RE = re.compile(
    r"[\[\]【】()（）<>〈〉「」『』]"
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def normalize_title(s: str | None) -> str:
    """Normalize a title for comparison and indexing.

    Pipeline: NFKC → OpenCC t2s → lowercase → whitespace collapse.
    Returns an empty string for falsy input.
    """
    if not s:
        return ""
    # NFKC: half-width → full-width, compatibility decomposition
    s = unicodedata.normalize("NFKC", s)
    # Traditional → Simplified Chinese
    cc = _get_cc()
    if cc is not None:
        s = cc.convert(s)
    # Lowercase
    s = s.lower()
    # Collapse whitespace
    s = " ".join(s.split())
    return s


def normalize_title_denoised(s: str | None) -> str:
    """Like ``normalize_title`` but also strips bracketed decorative content.

    Used for raw RSS titles that contain ``[字幕组]`` prefixes etc.
    """
    norm = normalize_title(s)
    if not norm:
        return ""
    # Remove bracketed segments — but only if they appear to be decorative
    # (subtitle group, resolution, codec, etc.).  We remove all bracketed
    # content because the clean title is what we want to match against.
    result = _BRACKET_CONTENT_RE.sub(" ", norm)
    return " ".join(result.split())


def _bigrams(s: str) -> set[str]:
    """Return the set of overlapping character bigrams of *s*."""
    if not s:
        return set()
    if len(s) < 2:
        return {s}
    return {s[i : i + 2] for i in range(len(s) - 1)}


def _levenshtein(a: str, b: str) -> int:
    """Compute Levenshtein edit distance between two strings."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        curr = [i]
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            curr.append(min(prev[j] + 1, curr[j - 1] + 1, prev[j - 1] + cost))
        prev = curr
    return prev[-1]


def _levenshtein_ratio(a: str, b: str) -> int:
    """Levenshtein-based similarity ratio (0–100), matching ``fuzz.ratio`` semantics."""
    if not a or not b:
        return 0
    if a == b:
        return 100
    dist = _levenshtein(a, b)
    total = len(a) + len(b)
    return round(100 * (total - dist) / total)


def similarity_score(a: str | None, b: str | None) -> int:
    """Similarity between two titles, scaled to 0–100.

    Combines bigram Dice (strong for CJK substring matching) with Levenshtein
    ratio (handles character transpositions in Latin scripts) and returns the
    max.  Both inputs are normalized (NFKC + OpenCC + lowercase) first.

    * Exact normalized match → 100.
    * Single-char containment → 90.
    * Otherwise: ``max(bigram_dice, levenshtein_ratio)``.
    """
    a_n = normalize_title(a)
    b_n = normalize_title(b)
    if not a_n or not b_n:
        return 0
    if a_n == b_n:
        return 100
    # Levenshtein ratio — handles transpositions, substitutions
    lev = _levenshtein_ratio(a_n, b_n)
    # Bigram Dice — strong for CJK, substring overlap
    a_bg = _bigrams(a_n)
    b_bg = _bigrams(b_n)
    if not a_bg or not b_bg:
        # One side is a single character — check containment
        if a_n in b_n or b_n in a_n:
            return 90
        return lev
    inter = len(a_bg & b_bg)
    dice = round(200 * inter / (len(a_bg) + len(b_bg)))
    return max(lev, dice)


def partial_similarity_score(a: str | None, b: str | None) -> int:
    """Best substring bigram Dice — replacement for ``fuzz.partial_ratio``.

    Finds the best alignment of the shorter string's bigrams within the longer
    string's bigrams.  Useful when one title is a prefix/substring of the other
    (e.g. ``"Attack on Titan Season 4"`` vs ``"Attack on Titan Season 4 Part 2"``).
    """
    a_n = normalize_title(a)
    b_n = normalize_title(b)
    if not a_n or not b_n:
        return 0
    if a_n == b_n:
        return 100
    # Containment check (one is substring of the other)
    if a_n in b_n or b_n in a_n:
        return 100
    # Sliding window: align shorter bigram set over longer string
    short, long_ = (a_n, b_n) if len(a_n) <= len(b_n) else (b_n, a_n)
    short_bg = _bigrams(short)
    if not short_bg:
        return 90 if short in long_ else 0
    long_bg = _bigrams(long_)
    inter = len(short_bg & long_bg)
    # Partial ratio: normalize by the shorter string's bigram count
    return round(100 * inter / len(short_bg))