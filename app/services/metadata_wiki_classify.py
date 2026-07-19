"""Wikipedia page classification (work vs non-work entity).

Pure leaf module - no DB, no LLM. Extracted verbatim from metadata_agent.py
(Phase 0 leaf extraction): classify a Wikipedia page from its categories +
summary as work/non_work/ambiguous, infer tv-vs-movie content type, and
validate a matched entity isn't a station/company/person page.
"""
from __future__ import annotations

import logging
import re

from app.services.metadata_resource_meta import ResourceMetadata

logger = logging.getLogger(__name__)


def _infer_content_type_from_categories(categories: list[str]) -> str:
    """Infer 'tv' vs 'movie' from a Wikipedia page's category list."""
    text = " ".join(categories or "").lower()
    if any(k in text for k in ("film", "movie", "電影", "电影", "短片")):
        return "movie"
    return "tv"


# ── Work-vs-non-work page classification ──────────────────────────────────
#
# A Wikipedia page for a TV *station* / broadcaster / streaming platform /
# company / person can title-match a fansub token (ViuTV, TVB, ...) with
# similarity >= AUTO_LINK_THRESHOLD. Title similarity alone therefore cannot
# tell a creative work from its broadcaster - that is how the ViuTV station
# page was auto-linked as a bogus series. The classifier below uses the page's
# category list (the strongest signal) with a summary lead-sentence fallback.

_WORK_CATEGORY_RE = re.compile(
    r"television series|television shows|tv series|tv shows|"
    r"\banime\b|anime and manga|anime television|animated television|"
    r"\bfilms\b|\bfilm\b|\bmovies\b|animated films|"
    r"manga|light novels|\bnovels\b|web series|"
    r"original video animation|\bova\b|original net animation|\bona\b|"
    r"电视剧|電視劇|動畫|动画|电影|電影|漫畫|漫画|"
    r"小說|小说|网络小说|網絡小說|中國小說|中国小说|中华人民共和国网络小说|"
    r"改编.*小说|改編.*小說|网络动画|網絡動畫|中国动画|中國動畫|"
    r"テレビアニメ|アニメ|映画|漫画",
    flags=re.IGNORECASE,
)
_NON_WORK_CATEGORY_RE = re.compile(
    r"disambiguation|set-index|set index|ambiguous|"
    r"television channels|television networks|television stations|"
    r"broadcasting|broadcasters|television programming blocks|"
    r"\bcompanies\b|\bcorporations\b|\bbrands\b|subsidiaries|"
    r"\bpeople\b|\bpersons\b|biographies|living people|\bbirths\b|\bdeaths\b|"
    r"voice actors|\bactors\b|\bdirectors\b|\bwriters\b|filmography|"
    r"albums|soundtracks|\bsongs\b|discographies|"
    r"\blists\b|\bstubs\b|"
    r"電視台|电视台|电视网|廣播|广播|公司|人物|消歧义|消歧義|"
    r"テレビ局|企業|人物|曖昧さ回避",
    flags=re.IGNORECASE,
)
_WORK_SUMMARY_RE = re.compile(
    r"(?:"
    r"\b(?:is|was|are|were)\b[^.]{0,60}\b"
    r"(?:television series|tv series|anime|animated series|film|movie|ova|ona|"
    r"manga|light novel|web series|drama series)\b"
    r"|"
    r"是[^。]{0,80}?(?:网络小说|小說|小说|网络动画|網絡動畫|动画|動畫|动漫|番剧|"
    r"电视剧|電視劇|連續劇|漫畫|漫画|電影|电影|作品)"
    r")",
    flags=re.IGNORECASE,
)
_NON_WORK_SUMMARY_RE = re.compile(
    r"(?:"
    r"\b(?:is|was|are|were)\b[^.]{0,60}\b"
    r"(?:television channel|television network|television station|broadcaster|"
    r"broadcasting|streaming service|streaming platform|video-on-demand|"
    r"video on demand|company|corporation|subsidiary|brand)\b"
    r"|"
    r"是[^。]{0,60}?(?:电视台|電視台|电视网|廣播|广播|公司|企業|品牌|人物|工作室|频道|頻道)"
    r")",
    flags=re.IGNORECASE,
)


def _classify_wikipedia_page(
    categories: list[str] | None, summary: str | None = None
) -> str:
    """Classify a Wikipedia page as ``"work"`` | ``"non_work"`` | ``"ambiguous"``.

    ``"work"`` => safe to link as a TVSeries/Movie. ``"non_work"`` => a
    station/network/platform/company/person/disambiguation page that must NOT
    be linked. ``"ambiguous"`` => not enough signal to decide; defer to the LLM
    judge. Categories dominate; the lead-sentence summary is a tiebreaker.
    """
    cat_text = " ".join(categories or [])
    has_non_work_cat = bool(cat_text and _NON_WORK_CATEGORY_RE.search(cat_text))
    has_work_cat = bool(cat_text and _WORK_CATEGORY_RE.search(cat_text))
    if has_non_work_cat and not has_work_cat:
        return "non_work"
    if has_non_work_cat and has_work_cat:
        return "ambiguous"  # mixed signals - let the judge decide
    if has_work_cat:
        return "work"
    text = summary or ""
    if _NON_WORK_SUMMARY_RE.search(text):
        return "non_work"
    if _WORK_SUMMARY_RE.search(text):
        return "work"
    return "ambiguous"


def _validate_matched_entity_kind(meta: ResourceMetadata) -> ResourceMetadata:
    """Defense-in-depth: decline a Wikipedia match whose page is a non-work
    entity (station / company / person / disambiguation), even if the agent
    returned it.

    B1 (auto-link gate) and B2 (judge prompt) already steer away from these,
    but a judge slip or a thin-categories fallthrough could still surface one -
    never upsert a bogus TVSeries from a non-work page. The reason phrasing
    deliberately avoids :data:`_NON_WORK_MARKERS` ("not a tv/movie/anime") so
    :func:`_classify_failure` treats it as a retryable ``not_found`` rather
    than a permanent ``non_work`` (the resource IS a show; we just matched the
    wrong page and should retry with better keywords).
    """
    me = getattr(meta, "matched_entity", None)
    if not meta.found or not me:
        return meta
    if (me.get("external_source") or "") != "wikipedia":
        return meta
    cats = me.get("categories") or []
    if not cats:
        return meta  # no categories to check (e.g. ReAct path) - trust B2
    kind = _classify_wikipedia_page(cats, me.get("description") or "")
    if kind == "non_work":
        logger.info(
            "[metadata_agent] declined non-work wikipedia match %r "
            "(categories=%s); downgrading to not_found",
            me.get("external_id"), cats[:3],
        )
        meta.found = False
        meta.matched_entity = None
        meta.confidence = 0.0
        meta.reason = (
            "declined non-work wikipedia entity match "
            "(channel/company/person); will retry"
        )
    return meta
