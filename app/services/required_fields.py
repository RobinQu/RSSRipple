"""Channel-level required metadata fields catalog + agent filter gating.

Pure leaf module - no DB. A channel declares which metadata fields its users
care about (``Channel.required_metadata_fields``). The declaration drives two
things:

1. Display: the channel resource list shows these fields per resource, shaped
   by each field's work-type applicability (:data:`APPLIES_TO`) — cells that
   are irrelevant for a row's shape render blank instead of a misleading "—"
   (e.g. batch episode ranges on movies).
2. Agent filter gating: an agent's ``filter_config`` / work ``filter_overrides``
   may only reference resource-level fields plus the DSL fields mapped from the
   declared keys.

Invariants (since the add-only policy):

- The list is **mandatory**: every channel carries at least the code-enforced
  baseline — base fields (:data:`BASE_REQUIRED_FIELDS`, required for every
  resource shape) plus the shape-scoped fields (:data:`SHAPE_REQUIRED_FIELDS`,
  e.g. episode for single-episode TV) — so they can never be cleared
  and there is no "unrestricted" state.
- The list is **add-only after creation**: the channel API rejects any update
  that would drop a previously-saved key.
- The catalog covers **every Filter DSL field**: resource-level DSL fields are
  catalog keys under their own name; work-namespaced DSL fields are grouped
  into series/movie pairs under one semantic key each.

Catalog layout is two-level: entries belong to a **section** (the work type
they apply to — base/tv/pack first, then cross-cutting release/work sections)
and within it to a semantic **group** (标题信息/集数信息/发布信息…). Both feed
the channel-form dialog's grouping.

Storage order is canonical (catalog definition order) so the stacked display
column renders deterministically; :func:`normalize_required_fields` enforces it.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from app.services.filter_engine import ALL_FIELDS, get_field_value, loaded_relation

# ── Row shapes ────────────────────────────────────────────────────────────
# Mirrors RowShape in frontend/src/utils/requiredFields.ts. Derived from which
# work/collection FK a resource carries; unlinked resources ("unknown") only
# match fields without an applies_to restriction.
TV_SINGLE = "tv_single"
TV_SEASON_BATCH = "tv_season_batch"
TV_MULTI_SEASON = "tv_multi_season"
TV_BATCH = "tv_batch"  # legacy shape name accepted by callers/tests
AUDIO = "audio"
FRANCHISE = "franchise"
MOVIE = "movie"

WORK_SHAPES: tuple[str, ...] = (TV_SINGLE, TV_SEASON_BATCH, TV_MULTI_SEASON, MOVIE)
CONTENT_TYPE_SHAPES: tuple[str, ...] = (*WORK_SHAPES, AUDIO)

# ── Lock scopes ───────────────────────────────────────────────────────────
# Code-enforced requirement tiers. ``always`` = required for every resource;
# scoped values = required whenever the resource has that shape. Locked keys
# are always present in a channel's stored list and can never be removed.
LOCK_ALWAYS = "always"
LOCK_TV_SINGLE = "tv_single"
LOCK_TV_BATCH = "tv_batch"
LOCK_FRANCHISE = "franchise"

# Ordered catalog: semantic key -> section + group + DSL mapping + lock scope
# + shape applicability. ``applies_to`` mirrors the frontend APPLICABILITY map:
# missing = relevant to every shape; work-pair keys need a linked work; episode
# machinery is TV-only and splits by single/batch.
REQUIRED_FIELD_CATALOG: dict[str, dict[str, Any]] = {
    # ── Section「基础必选」：全部形态适用 ──
    "title_cn": {
        "section": "base",
        "group": "title",
        "dsl_fields": ["title_cn"],
        # Chinese localization is optional. Legacy channels that already
        # stored this key retain it through the channel add-only policy.
        "lock": None,
    },
    "title_en": {
        "section": "base",
        "group": "title",
        "dsl_fields": ["title_en"],
        "lock": None,
    },
    "search_title": {
        "section": "base",
        "group": "title",
        "dsl_fields": ["search_title"],
        "lock": LOCK_ALWAYS,
    },
    "content_type": {
        "section": "base",
        "group": "work_type",
        "dsl_fields": ["content_type"],
        "lock": LOCK_ALWAYS,
        # A franchise collection can mix TV, movies, OVA and extras and has
        # no flat work FK by invariant, so no single tv/movie/audio value is
        # semantically valid for that row shape.
        "applies_to": CONTENT_TYPE_SHAPES,
    },
    "is_batch": {
        "section": "base",
        "group": "work_type",
        "dsl_fields": ["is_batch"],
        "lock": LOCK_ALWAYS,
    },
    "year": {
        "section": "base",
        "group": "work",
        # Release year of the linked work (series./movie. pair).
        "dsl_fields": ["series.year", "movie.year"],
        "lock": LOCK_ALWAYS,
        "applies_to": WORK_SHAPES,
    },
    "is_anime": {
        "section": "base",
        "group": "classification",
        # Anime flag of the linked work (series./movie. pair).
        "dsl_fields": ["series.is_anime", "movie.is_anime"],
        "lock": LOCK_ALWAYS,
        "applies_to": WORK_SHAPES,
    },
    # ── Section「剧集作品」：集数机制仅对 TV 资源有意义 ──
    # 作品单季化（per-season works）：季号由作品身份承载（TVSeries =
    # 恰好一季），``season`` 从形态必填退役为可选声明；DSL 字段本身保留
    # （资源解析证据）。``absolute_episode`` / ``episode_confidence`` 两键已
    # 退役出目录（字段仍存在于资源模型与 DSL）。
    "season": {
        "section": "tv",
        "group": "episode",
        "dsl_fields": ["season"],
        "lock": None,
        "applies_to": (TV_SINGLE, TV_SEASON_BATCH),
    },
    "episode": {
        "section": "tv",
        "group": "episode",
        "dsl_fields": ["episode"],
        "lock": LOCK_TV_SINGLE,
        "applies_to": (TV_SINGLE,),
    },
    "episode_start": {
        "section": "tv",
        "group": "episode",
        "dsl_fields": ["episode_start"],
        "lock": LOCK_TV_BATCH,
        "applies_to": (TV_SEASON_BATCH,),
    },
    "episode_end": {
        "section": "tv",
        "group": "episode",
        "dsl_fields": ["episode_end"],
        "lock": LOCK_TV_BATCH,
        "applies_to": (TV_SEASON_BATCH,),
    },
    # ── Section「多作品合集」：franchise 包专属 ──
    "resource_collection": {
        "section": "pack",
        "group": "pack",
        # Resource-level franchise-pack collection display name (DSL field
        # ``collection``); distinct from the work-pair ``collection`` key below.
        "dsl_fields": ["collection"],
        "lock": LOCK_FRANCHISE,
        "applies_to": (FRANCHISE,),
    },
    # ── Section「发布信息」：跨形态可选 ──
    "subtitle_group": {
        "section": "release", "group": "release",
        # Keep the catalog key stable for add-only channel declarations while
        # allowing the canonical plural DSL field alongside the legacy alias.
        "dsl_fields": ["subtitle_group", "subtitle_groups"], "lock": None,
    },
    "resolution": {"section": "release", "group": "release", "dsl_fields": ["resolution"], "lock": None},
    "source": {"section": "release", "group": "release", "dsl_fields": ["source"], "lock": None},
    "video_codec": {"section": "release", "group": "release", "dsl_fields": ["video_codec"], "lock": None},
    "audio_codec": {"section": "release", "group": "release", "dsl_fields": ["audio_codec"], "lock": None},
    "subtitle_type": {"section": "release", "group": "release", "dsl_fields": ["subtitle_type"], "lock": None},
    "subtitle_langs": {"section": "release", "group": "release", "dsl_fields": ["subtitle_langs"], "lock": None},
    "container": {"section": "release", "group": "release", "dsl_fields": ["container"], "lock": None},
    "file_size": {"section": "release", "group": "release", "dsl_fields": ["file_size"], "lock": None},
    # ── Section「作品信息」：跨形态可选，需链接作品 ──
    "rating": {
        "section": "work",
        "group": "work",
        "dsl_fields": ["series.rating", "movie.rating"],
        "lock": None,
        "applies_to": WORK_SHAPES,
    },
    "genre": {
        "section": "work",
        "group": "classification",
        "dsl_fields": ["series.genre", "movie.genre"],
        "lock": None,
        "applies_to": WORK_SHAPES,
    },
    "collection": {
        "section": "work",
        "group": "classification",
        "dsl_fields": ["series.collection", "movie.collection"],
        "lock": None,
        "applies_to": WORK_SHAPES,
    },
}

# Section keys in display order — work-type grouping first (基础必选 → 剧集作品
# → 多作品合集), then cross-cutting optional categories.
REQUIRED_FIELD_SECTIONS: tuple[str, ...] = ("base", "tv", "pack", "release", "work")

# Semantic group keys in display order (sub-grouping inside each section).
REQUIRED_FIELD_GROUPS: tuple[str, ...] = (
    "title",
    "work_type",
    "work",
    "classification",
    "episode",
    "pack",
    "release",
)


def _locked_keys() -> frozenset[str]:
    return frozenset(k for k, e in REQUIRED_FIELD_CATALOG.items() if e["lock"] is not None)


# Code-enforced baseline forced into EVERY new channel's stored list: the always
# required base fields plus the shape-scoped ones. They can never be cleared —
# hence there is no "unrestricted" configuration. (The list itself is add-only
# after channel creation. One-time migrations remove keys inherited solely
# from older baselines; users may explicitly opt into those keys again.
LOCKED_REQUIRED_FIELDS: frozenset[str] = _locked_keys()

# Base tier: required regardless of the resource's work type.
BASE_REQUIRED_FIELDS: frozenset[str] = frozenset(
    k for k, e in REQUIRED_FIELD_CATALOG.items() if e["lock"] == LOCK_ALWAYS
)

# Shape tier: lock scope -> keys required for resources of that shape.
# Franchise packs don't take any TV episode machinery; multi-season packs
# (works carried on the link table) take none either — their coverage gate
# lives in resource_confirmation.
_SHAPE_LOCKS: dict[str, str] = {
    TV_SINGLE: LOCK_TV_SINGLE,
    TV_SEASON_BATCH: LOCK_TV_BATCH,
    TV_BATCH: LOCK_TV_BATCH,
    TV_MULTI_SEASON: "",
    FRANCHISE: LOCK_FRANCHISE,
}
SHAPE_REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    shape: tuple(
        k for k, e in REQUIRED_FIELD_CATALOG.items() if scope and e["lock"] == scope
    )
    for shape, scope in _SHAPE_LOCKS.items()
}


def required_keys_for_shape(shape: str) -> frozenset[str]:
    """Effective required catalog keys for one resource shape.

    Base keys restricted by ``applies_to`` only count when the shape matches
    (e.g. year/is_anime need a linked work, so franchise packs don't require
    them); shape-scoped locks add their tier on top.
    """

    def _applies(entry: dict[str, Any]) -> bool:
        applies = entry.get("applies_to")
        return not applies or shape in applies

    return frozenset(
        k
        for k, e in REQUIRED_FIELD_CATALOG.items()
        if e["lock"] == LOCK_ALWAYS and _applies(e)
    ) | frozenset(SHAPE_REQUIRED_FIELDS.get(shape, ()))


def resource_shape(resource: Any) -> str | None:
    """Return the required-field row shape for a file resource.

    Unlinked resources have no supported work shape. Audio resources use only
    universally applicable Channel fields; TV/movie-only fields are skipped.
    Terminal multi-season packs clear the flat work FK (works live on the
    link table) — they are shaped by ``batch_scope`` alone.
    """
    if getattr(resource, "audio_work_id", None):
        return AUDIO
    scope = getattr(resource, "batch_scope", None)
    if getattr(resource, "collection_id", None) or scope == "franchise":
        return FRANCHISE
    if getattr(resource, "movie_id", None):
        return MOVIE
    if getattr(resource, "series_id", None):
        if not getattr(resource, "is_batch", False):
            return TV_SINGLE
        if scope == "multi_season":
            return TV_MULTI_SEASON
        if scope == "season":
            return TV_SEASON_BATCH
        return None
    if scope == "multi_season" and getattr(resource, "is_batch", False):
        return TV_MULTI_SEASON
    return None


def applicable_required_fields(
    keys: list[str] | None, shape: str
) -> tuple[str, ...]:
    """Return declared required keys that apply to ``shape`` in catalog order."""
    selected = set(normalize_required_fields(keys))
    return tuple(
        key
        for key, entry in REQUIRED_FIELD_CATALOG.items()
        if key in selected
        and (not entry.get("applies_to") or shape in entry["applies_to"])
    )


def _linked_work(resource: Any) -> tuple[str | None, Any | None]:
    """First work reachable through the multi-work link table.

    Terminal multi-season packs clear the flat work FKs and carry their works
    in ``resource_work_links``. Only already-loaded relationships are
    consulted (async sessions cannot lazy-load); an unloaded link table
    yields ``(None, None)``.
    """
    links = loaded_relation(resource, "work_links")
    if not links:
        return None, None
    for link in links:
        if getattr(link, "series_id", None):
            return "series", loaded_relation(link, "series")
        if getattr(link, "movie_id", None):
            return "movie", loaded_relation(link, "movie")
    return None, None


def _semantic_field_value(resource: Any, key: str) -> Any:
    """Resolve one catalog key to the value relevant to this resource."""
    fields = REQUIRED_FIELD_CATALOG[key]["dsl_fields"]
    if len(fields) == 1:
        value = get_field_value(resource, fields[0])
        if value is None and fields[0] == "content_type":
            # Links-carried packs: derive the medium from the link table.
            kind, _work = _linked_work(resource)
            if kind is not None:
                return "tv" if kind == "series" else "movie"
        return value
    if getattr(resource, "series_id", None):
        field = next((f for f in fields if f.startswith("series.")), fields[0])
        return get_field_value(resource, field)
    if getattr(resource, "movie_id", None):
        field = next((f for f in fields if f.startswith("movie.")), fields[0])
        return get_field_value(resource, field)
    kind, work = _linked_work(resource)
    if kind is None or work is None:
        return None
    field = next((f for f in fields if f.startswith(f"{kind}.")), fields[0])
    return get_field_value(SimpleNamespace(**{kind: work}), field)


def missing_required_fields(
    resource: Any, required_metadata_fields: list[str] | None
) -> list[str]:
    """Return applicable Channel-required fields whose resource value is empty.

    ``False`` and numeric zero are valid values.  Blank strings and empty
    collections are missing, matching the Filter DSL's empty-value semantics.
    """
    shape = resource_shape(resource)
    if shape is None:
        return []
    missing: list[str] = []
    for key in applicable_required_fields(required_metadata_fields, shape):
        value = _semantic_field_value(resource, key)
        if value is None:
            missing.append(key)
        elif isinstance(value, str) and not value.strip():
            missing.append(key)
        elif isinstance(value, (list, tuple, set)) and not value:
            missing.append(key)
    return missing


# Resource-level fields are always allowed in agent filters — they come from
# the title pre-parser / fetch, not from metadata matching luck.
_RESOURCE_LEVEL_FIELDS: frozenset[str] = frozenset(
    f for f in ALL_FIELDS if not f.startswith(("series.", "movie."))
)


def validate_required_fields(keys: list[str] | None) -> list[str]:
    """Validate catalog keys; returns a list of error strings (empty = ok)."""
    errors: list[str] = []
    for key in keys or []:
        if key not in REQUIRED_FIELD_CATALOG:
            errors.append(f"unknown required metadata field: {key!r}")
    return errors


def normalize_required_fields(keys: list[str] | None) -> list[str]:
    """Union the submitted keys with the locked baseline, dedupe, and reorder
    into canonical catalog order.

    Every stored ``required_metadata_fields`` value must pass through this so
    the invariant "baseline always present" holds regardless of caller, and
    the stacked display column renders deterministically.
    """
    merged: dict[str, None] = {}
    for key in list(keys or []) + sorted(LOCKED_REQUIRED_FIELDS):
        if key in REQUIRED_FIELD_CATALOG:
            merged.setdefault(key)
    return [key for key in REQUIRED_FIELD_CATALOG if key in merged]


def allowed_agent_filter_fields(
    required_metadata_fields: list[str] | None,
) -> frozenset[str] | None:
    """Fields an agent may use in filter DSL, given its channel's declared
    ``required_metadata_fields`` value.

    Returns None when the channel has no declaration — a legacy/defensive
    fallback only: the light migration backfills every row to at least the
    locked baseline, so post-migration values are always lists. Otherwise the
    allowed set is the resource-level universe plus the DSL fields mapped from
    the declared keys.
    """
    keys = required_metadata_fields
    if keys is None:
        return None
    allowed = set(_RESOURCE_LEVEL_FIELDS)
    for key in keys:
        entry = REQUIRED_FIELD_CATALOG.get(key)
        if entry:
            allowed.update(entry["dsl_fields"])
    return frozenset(allowed)


def _collect_fields(node: Any, out: set[str]) -> None:
    if not isinstance(node, dict):
        return
    if "combinator" in node and "conditions" in node:
        for child in node.get("conditions") or []:
            _collect_fields(child, out)
    elif "field" in node:
        field = node.get("field")
        if isinstance(field, str):
            out.add(field)


def validate_filter_against_allowed(
    filter_config: Any, allowed: frozenset[str]
) -> list[str]:
    """Report DSL fields outside *allowed* (empty list = ok)."""
    used: set[str] = set()
    _collect_fields(filter_config, used)
    return [
        f"field {f!r} is not in the channel's required metadata fields"
        for f in sorted(used - allowed)
    ]


__all__ = [
    "BASE_REQUIRED_FIELDS",
    "LOCKED_REQUIRED_FIELDS",
    "REQUIRED_FIELD_CATALOG",
    "REQUIRED_FIELD_GROUPS",
    "REQUIRED_FIELD_SECTIONS",
    "SHAPE_REQUIRED_FIELDS",
    "allowed_agent_filter_fields",
    "applicable_required_fields",
    "missing_required_fields",
    "normalize_required_fields",
    "resource_shape",
    "required_keys_for_shape",
    "validate_filter_against_allowed",
    "validate_required_fields",
]
