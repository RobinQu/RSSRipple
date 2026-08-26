"""Authoritative registry of external metadata identity sites.

Single source of truth for the 7 authoritative sites (two-source channel
architecture, Phase P1): wikipedia, tmdb, bangumi, mal, anilist, imdb,
douban. ``baidu_baike`` and ``eiga`` were dropped from the identity scheme.

Each entry declares:
  * ``name`` — the canonical source tag used in ``external_source`` and the
    ``source:id`` canonical id form;
  * ``label`` — display name for UI links;
  * ``canonical_id_form`` — human-readable documentation of the id shape;
  * a URL → (source, id) extractor (host suffix + path regex), used by the
    web-search fallback to pin a stable identity onto an LLM-chosen page;
  * a display link template (TMDB is content-type aware: /tv/ vs /movie/).

Provides:
  * :func:`canonicalize_external_id` — primary-id normalization (moved here
    from ``metadata_service``; that module re-exports it for compat);
  * :func:`source_and_id_from_url` — URL → (source, "source:id");
  * :func:`build_source_links` — server-side display links for work detail
    pages (replaces the frontend ``sourceLinks.ts`` helper).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import unquote, urlparse

# Registry order is also the default web-search fallback whitelist order:
# anime-centric DBs first, then general DBs, then identity-only databases.
DEFAULT_FALLBACK_SOURCES: list[str] = [
    "bangumi", "mal", "anilist", "tmdb", "wikipedia", "imdb", "douban",
]

# Registry site -> search-engine domain filters for the wigolo fallback's
# ``include_domains`` push-down. Subdomain matches count (bangumi.tv covers
# its mirrors' hosts; wikipedia.org covers every language edition; douban.com
# covers movie.douban.com).
SITE_DOMAINS: dict[str, list[str]] = {
    "wikipedia": ["wikipedia.org"],
    "tmdb": ["themoviedb.org"],
    "bangumi": ["bangumi.tv", "bgm.tv"],
    "mal": ["myanimelist.net"],
    "anilist": ["anilist.co"],
    "imdb": ["imdb.com"],
    "douban": ["douban.com"],
}


def domains_for_sources(sources: list[str] | None = None) -> list[str]:
    """Search-engine domain allowlist for the given registry sites.

    ``None``/empty means the default whitelist order. Unknown site names are
    skipped (they have no domain mapping); duplicates are dropped.
    """
    names = sources if sources else DEFAULT_FALLBACK_SOURCES
    out: list[str] = []
    for name in names:
        for dom in SITE_DOMAINS.get(name, []):
            if dom not in out:
                out.append(dom)
    return out


@dataclass(frozen=True)
class SourceSpec:
    name: str
    label: str
    canonical_id_form: str
    host_pattern: str
    id_pattern: re.Pattern | None = None
    # link_template: "{id}" is the extracted id, "{seg}" is tmdb's tv/movie
    # path segment (content-type aware).
    link_template: str | None = None
    extra: dict = field(default_factory=dict)


_SOURCE_SPECS: tuple[SourceSpec, ...] = (
    SourceSpec(
        name="wikipedia",
        label="Wikipedia",
        canonical_id_form=(
            "wikipedia:{lang}:{page_id} (numeric; legacy bare "
            "wikipedia:{page_id}; slug form from URL extraction)"
        ),
        host_pattern=r"(^|\.)wikipedia\.org$",
        id_pattern=re.compile(r"/wiki/(?P<id>[^/?#]+)"),
        # Numeric page ids link via curid; slug ids have no stable link.
        link_template="https://en.wikipedia.org/?curid={id}",
    ),
    SourceSpec(
        name="tmdb",
        label="TMDB",
        canonical_id_form="tmdb:{digits}",
        host_pattern=r"(^|\.)themoviedb\.org$",
        id_pattern=re.compile(r"/(?:tv|movie)/(?P<id>\d+)"),
        link_template="https://www.themoviedb.org/{seg}/{id}",
    ),
    SourceSpec(
        name="bangumi",
        label="Bangumi",
        canonical_id_form="bangumi:{digits}",
        host_pattern=r"(^|\.)(bangumi|bgm)\.tv$",
        id_pattern=re.compile(r"/subject/(?P<id>\d+)"),
        link_template="https://bangumi.tv/subject/{id}",
    ),
    SourceSpec(
        name="mal",
        label="MyAnimeList",
        canonical_id_form="mal:{digits}",
        host_pattern=r"(^|\.)myanimelist\.net$",
        id_pattern=re.compile(r"/anime/(?P<id>\d+)"),
        link_template="https://myanimelist.net/anime/{id}",
    ),
    SourceSpec(
        name="anilist",
        label="AniList",
        canonical_id_form="anilist:{digits}",
        host_pattern=r"(^|\.)anilist\.co$",
        id_pattern=re.compile(r"/anime/(?P<id>\d+)"),
        link_template="https://anilist.co/anime/{id}",
    ),
    SourceSpec(
        name="imdb",
        label="IMDb",
        canonical_id_form="imdb:{tt_digits}",
        host_pattern=r"(^|\.)imdb\.com$",
        id_pattern=re.compile(r"/(?:title/)?(?P<id>tt\d+)"),
        link_template="https://www.imdb.com/title/{id}/",
    ),
    SourceSpec(
        name="douban",
        label="豆瓣",
        canonical_id_form="douban:{digits}",
        host_pattern=r"(^|\.)douban\.com$",
        id_pattern=re.compile(r"/subject/(?P<id>\d+)"),
        link_template="https://movie.douban.com/subject/{id}/",
    ),
)

REGISTRY: dict[str, SourceSpec] = {s.name: s for s in _SOURCE_SPECS}
# Names of the 7 authoritative sites (validation set for channel fallback
# whitelists and any other "registry source" checks).
REGISTRY_SOURCES: frozenset[str] = frozenset(REGISTRY)


def _link_for(spec: SourceSpec, raw_id: str, content_type: str | None) -> str | None:
    if not spec.link_template:
        return None
    if spec.name == "wikipedia" and not raw_id.isdigit():
        return None  # slug-form ids have no stable display link
    seg = "movie" if content_type == "movie" else "tv"
    return spec.link_template.format(id=raw_id, seg=seg)


# ---------------------------------------------------------------------------
# Wikipedia id forms
#
# Wikipedia pageids are PER-LANGUAGE-EDITION (zh/en/ja each number their own
# pages), so the canonical storage form carries the edition:
# ``wikipedia:{lang}:{pageid}`` (e.g. ``wikipedia:zh:7301786``). Rows written
# before the lang qualifier existed use the legacy bare form
# ``wikipedia:{pageid}``; lookups must accept both, and the bare form's
# display link is unreliable (the edition is unrecoverable without an API
# call — the backfill script rewrites it).
# ---------------------------------------------------------------------------

_WIKI_QUALIFIED_RE = re.compile(r"^wikipedia:([a-z][a-z-]*):(\d{1,12})$")
_WIKI_BARE_RE = re.compile(r"^wikipedia:(\d{1,12})$")
# ``{lang}.wikipedia.org`` host, e.g. from a matched page's fullurl.
_WIKI_URL_LANG_RE = re.compile(r"(?:^|\.)([a-z][a-z-]*)\.wikipedia\.org$")


def parse_wikipedia_id(external_id: str | None) -> tuple[str | None, str | None]:
    """Parse a wikipedia external id into (lang, pageid) for numeric forms.

    ``wikipedia:zh:7301786`` → ``("zh", "7301786")``;
    ``wikipedia:7301786`` (legacy bare) → ``(None, "7301786")``;
    slug forms (``wikipedia:Some_Title``) and non-wikipedia ids → ``(None, None)``.
    """
    if not external_id:
        return None, None
    m = _WIKI_QUALIFIED_RE.match(external_id.strip().lower())
    if m:
        return m.group(1), m.group(2)
    m = _WIKI_BARE_RE.match(external_id.strip().lower())
    if m:
        return None, m.group(1)
    return None, None


def qualify_wikipedia_id(
    external_id: str | None,
    *,
    lang: str | None = None,
    wikipedia_url: str | None = None,
) -> str | None:
    """Return the language-qualified form of a numeric wikipedia id.

    Already-qualified ids pass through; the edition comes from the explicit
    ``lang`` (the wiki the page was fetched from) or, failing that, the
    ``{lang}.wikipedia.org`` host of ``wikipedia_url``. Bare ids whose edition
    cannot be determined, slug ids, and non-wikipedia ids are returned
    unchanged.
    """
    if not external_id:
        return external_id
    s = external_id.strip().lower()
    id_lang, pid = parse_wikipedia_id(s)
    if pid is None or id_lang is not None:
        return s
    if not lang and wikipedia_url:
        m = _WIKI_URL_LANG_RE.search((urlparse(wikipedia_url).hostname or "").lower())
        lang = m.group(1) if m else None
    if not lang:
        return s
    return f"wikipedia:{lang}:{pid}"


def wikipedia_match_keys(external_id: str | None) -> tuple[list[str], str | None]:
    """Lookup keys for a wikipedia id across both storage forms.

    Returns ``(exact_keys, like_pattern)``: a qualified id matches its own
    form plus the legacy bare form exactly; a bare id matches itself exactly
    plus any qualified form via the LIKE pattern ``wikipedia:%:{pageid}``
    (pageids are digits-only, so the pattern holds no user-controlled
    wildcards). Non-numeric ids match only themselves.
    """
    lang, pid = parse_wikipedia_id(external_id)
    if pid is None:
        return [external_id] if external_id else [], None
    if lang:
        return [f"wikipedia:{lang}:{pid}", f"wikipedia:{pid}"], None
    return [f"wikipedia:{pid}"], f"wikipedia:%:{pid}"


# ---------------------------------------------------------------------------
# external_id canonicalization
#
# Exa Agent Search returns TMDB ids in inconsistent shapes:
#   "TMDB:82684", "TMDB 82684", "TMDB TV 82684 / season 4", "82684"
# All of them refer to the same TMDB work, but our naive `external_id`
# lookup would treat each shape as a separate row and keep spawning
# duplicate TVSeries/Movie entities on every fetch.
# The canonicalizer collapses those shapes into a single canonical form
# (e.g. ``tmdb:82684``) so upserts converge.
# ---------------------------------------------------------------------------

_TMDB_DIGITS_RE = re.compile(r"tmdb[^0-9]*(\d{2,10})", re.IGNORECASE)
_LEADING_DIGITS_RE = re.compile(r"^\s*(\d{2,10})\s*$")


def canonicalize_external_id(
    raw_id: str | None,
    source: str | None,
    content_type: str | None = None,
) -> str | None:
    """Return a stable canonical form of ``raw_id`` for upsert matching.

    Rules:
      * Any string containing ``tmdb`` and digits → ``tmdb:{digits}``.
      * ``source == "tmdb"`` combined with a pure-digit id → ``tmdb:{digits}``.
      * IMDb ids (``tt`` + digits) → ``imdb:{tt…}``.
      * Otherwise: lowercase + collapse whitespace, and drop known clutter
        such as ``/ season N`` tails. Already-canonical ``source:id`` forms
        for the other registry sites (wikipedia/bangumi/mal/anilist/douban)
        pass through this rule unchanged.

    Never fabricates an id; returns None only when the input is falsy.
    """
    if raw_id is None:
        return None
    s = str(raw_id).strip()
    if not s:
        return None

    # Strip trailing "/ season N" or similar decoration.
    s_clean = re.sub(r"[\s/,;|]+season[\s#:_-]*\d+\s*$", "", s, flags=re.IGNORECASE)
    s_clean = re.sub(r"\s+", " ", s_clean).strip()

    lower = s_clean.lower()
    # TMDB detection — any "tmdb" prefix or when source declares tmdb.
    if "tmdb" in lower:
        m = _TMDB_DIGITS_RE.search(lower)
        if m:
            return f"tmdb:{m.group(1)}"
    if (source or "").strip().lower() == "tmdb":
        m = _LEADING_DIGITS_RE.match(lower)
        if m:
            return f"tmdb:{m.group(1)}"
        m = re.search(r"(\d{2,10})", lower)
        if m:
            return f"tmdb:{m.group(1)}"

    # IMDb ids
    m = re.match(r"^(tt\d{5,})$", lower)
    if m:
        return f"imdb:{m.group(1)}"

    return lower or None


# ---------------------------------------------------------------------------
# URL → (source, "source:id") extraction
# ---------------------------------------------------------------------------

def source_and_id_from_url(url: str) -> tuple[str, str] | None:
    """Map an authoritative media-DB URL to (source, "source:id").

    Returns None for unrecognised pages; callers (e.g. the web-search
    fallback) decide what the no-identity marker should be. Percent-encoded
    URLs are decoded first — search engines (notably keyword engines like
    wigolo's bing/ddg pool) frequently return encoded wiki slugs, and the
    extracted ``external_id`` must converge with its decoded form.
    """
    parsed = urlparse(unquote(url or ""))
    host = (parsed.hostname or "").lower()
    path = parsed.path or ""
    for spec in _SOURCE_SPECS:
        if not spec.id_pattern or not re.search(spec.host_pattern, host):
            continue
        m = spec.id_pattern.search(path)
        if not m:
            continue
        raw_id = m.group("id")
        return spec.name, f"{spec.name}:{raw_id}"
    return None


# ---------------------------------------------------------------------------
# Display links for work detail pages
# ---------------------------------------------------------------------------

def _wikipedia_link(external_id: str) -> dict | None:
    """Display link for a numeric wikipedia id, language-edition aware.

    Qualified ids (``wikipedia:zh:7301786``) link to their own edition's
    curid and are labelled with the language (``Wikipedia (zh)``) so the
    per-language pages of one work are distinguishable. Legacy bare ids keep
    the historical en-edition curid link (their edition is unrecoverable
    offline; the backfill script rewrites them). Slug ids have no stable
    link.
    """
    lang, pid = parse_wikipedia_id(external_id)
    if pid is None:
        return None
    if lang:
        return {
            "source": "wikipedia",
            "label": f"Wikipedia ({lang})",
            "url": f"https://{lang}.wikipedia.org/?curid={pid}",
        }
    return {
        "source": "wikipedia",
        "label": "Wikipedia",
        "url": f"https://en.wikipedia.org/?curid={pid}",
    }


def build_source_links(
    external_id: str | None,
    external_source: str | None,
    content_type: str | None,
    wikipedia_url: str | None = None,
    extra_ids: list[str] | None = None,
) -> list[dict]:
    """Build display links for a work from its identity fields.

    Handles the legacy compound form (``TMDB:632617; IMDb:tt10986222`` — ids
    combined in one string, split on ';'/'|'), canonical ``source:id`` forms
    for all 7 registry sites, and a bare digit id with
    ``external_source == "tmdb"``. ``extra_ids`` (P3) carries the identity
    bag's secondary canonical ids (already deduped against the primary by the
    caller) so a work shows every known authoritative link. Returns
    ``[{source, label, url}]``, deduped by (source, url) preserving order.
    """
    links: list[dict] = []
    has_wikipedia = False

    if wikipedia_url:
        m = _WIKI_URL_LANG_RE.search((urlparse(wikipedia_url).hostname or "").lower())
        label = f"Wikipedia ({m.group(1)})" if m else "Wikipedia"
        links.append({"source": "wikipedia", "label": label, "url": wikipedia_url})
        has_wikipedia = True

    if external_id:
        declared = (external_source or "").strip().lower()
        for part in re.split(r"[;|]", external_id):
            token = part.strip()
            if not token:
                continue
            # Legacy explicit forms: tmdb / imdb / wikipedia prefixes.
            m = re.search(r"tmdb[:：\s]*(\d+)", token, flags=re.IGNORECASE)
            if m:
                links.append({
                    "source": "tmdb", "label": "TMDB",
                    "url": _link_for(REGISTRY["tmdb"], m.group(1), content_type),
                })
                continue
            m = re.search(r"imdb[:：\s]*(tt\d+)", token, flags=re.IGNORECASE) or re.match(
                r"^(tt\d+)$", token, flags=re.IGNORECASE
            )
            if m:
                links.append({
                    "source": "imdb", "label": "IMDb",
                    "url": _link_for(REGISTRY["imdb"], m.group(1), content_type),
                })
                continue
            if token.lower().startswith(("wikipedia:", "wikipedia：")):
                if not has_wikipedia:
                    link = _wikipedia_link(token.lower().replace("：", ":"))
                    if link:
                        links.append(link)
                        has_wikipedia = True
                continue
            # Canonical "source:id" form for the other registry sites.
            m = re.match(r"^([a-z_]+)[:：](.+)$", token)
            if m and m.group(1) in REGISTRY and m.group(1) != "wikipedia":
                spec = REGISTRY[m.group(1)]
                url = _link_for(spec, m.group(2).strip(), content_type)
                if url:
                    links.append({"source": spec.name, "label": spec.label, "url": url})
                continue
            # source-declared bare id, e.g. external_source='tmdb', external_id='632617'
            if declared == "tmdb" and token.isdigit():
                links.append({
                    "source": "tmdb", "label": "TMDB",
                    "url": _link_for(REGISTRY["tmdb"], token, content_type),
                })

    # P3: identity-bag secondary ids (canonical "source:id" only; the bag
    # convention guarantees the form). Unlike the primary wikipedia token,
    # each bag pageid gets its own curid link — langlink pageids are distinct
    # pages and all worth showing, labelled per language edition. Final
    # dedupe removes any repeat of the primary link.
    for token in (extra_ids or []):
        token = token.strip()
        if token.lower().startswith(("wikipedia:", "wikipedia：")):
            link = _wikipedia_link(token.lower().replace("：", ":"))
            if link:
                links.append(link)
            continue
        m = re.match(r"^([a-z_]+)[:：](.+)$", token)
        if not m or m.group(1) not in REGISTRY:
            continue
        spec = REGISTRY[m.group(1)]
        url = _link_for(spec, m.group(2).strip(), content_type)
        if url:
            links.append({"source": spec.name, "label": spec.label, "url": url})

    # Dedupe by (source, url), preserving order.
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    for link in links:
        if not link.get("url"):
            continue
        key = (link["source"], link["url"])
        if key in seen:
            continue
        seen.add(key)
        out.append(link)
    return out


__all__ = [
    "DEFAULT_FALLBACK_SOURCES",
    "REGISTRY",
    "REGISTRY_SOURCES",
    "SITE_DOMAINS",
    "SourceSpec",
    "build_source_links",
    "canonicalize_external_id",
    "domains_for_sources",
    "parse_wikipedia_id",
    "qualify_wikipedia_id",
    "source_and_id_from_url",
    "wikipedia_match_keys",
]
