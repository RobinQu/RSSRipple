"""Tests for required_fields: catalog validation, normalization, shape-aware
requirement tiers, allowed-field computation, and filter-tree gating."""

from app.services.filter_engine import ALL_FIELDS
from app.services.required_fields import (
    BASE_REQUIRED_FIELDS,
    LOCKED_REQUIRED_FIELDS,
    REQUIRED_FIELD_CATALOG,
    REQUIRED_FIELD_GROUPS,
    REQUIRED_FIELD_SECTIONS,
    SHAPE_REQUIRED_FIELDS,
    allowed_agent_filter_fields,
    normalize_required_fields,
    required_keys_for_shape,
    validate_filter_against_allowed,
    validate_required_fields,
)

# ---------------------------------------------------------------------------
# Catalog coverage invariants
# ---------------------------------------------------------------------------


def test_catalog_covers_every_dsl_field():
    """Every Filter DSL field must be selectable as a required field."""
    covered = {
        f for entry in REQUIRED_FIELD_CATALOG.values() for f in entry["dsl_fields"]
    }
    assert covered == set(ALL_FIELDS)


def test_base_tier_is_seven_always_required_fields():
    """基础必选：标题×3 + 作品类型/是否合集 + 发行年份/动漫判定。"""
    assert BASE_REQUIRED_FIELDS == frozenset({
        "title_cn", "title_en", "search_title",
        "content_type", "is_batch", "year", "is_anime",
    })
    for key in BASE_REQUIRED_FIELDS:
        assert REQUIRED_FIELD_CATALOG[key]["lock"] == "always"


def test_shape_scoped_locks():
    """TV 单集必填 season+episode；TV 合集另需起止集；多作品合集必填关联。"""
    assert SHAPE_REQUIRED_FIELDS["tv_single"] == ("season", "episode")
    # season (LOCK_TV) precedes the batch-specific range keys.
    assert SHAPE_REQUIRED_FIELDS["tv_batch"] == (
        "season", "episode_start", "episode_end",
    )
    assert SHAPE_REQUIRED_FIELDS["franchise"] == ("resource_collection",)
    assert SHAPE_REQUIRED_FIELDS.get("movie", ()) == ()
    # Shape-scoped keys carry the matching applies_to restriction.
    for entry in REQUIRED_FIELD_CATALOG.values():
        if entry["lock"] not in (None, "always"):
            assert entry["applies_to"], f"{entry} lock without applies_to"


def test_locked_storage_baseline_unions_base_and_shape_tiers():
    """Every locked key is forced into each channel's stored list."""
    expected = BASE_REQUIRED_FIELDS | {
        k
        for keys in SHAPE_REQUIRED_FIELDS.values()
        for k in keys
    }
    assert LOCKED_REQUIRED_FIELDS == expected


def test_required_keys_per_shape_respects_applies_to():
    """year/is_anime need a linked work — franchise packs don't require them."""
    tv_single = required_keys_for_shape("tv_single")
    assert BASE_REQUIRED_FIELDS <= tv_single | set()
    assert {"season", "episode"} <= tv_single
    # year/is_anime are work-scoped base fields; unlinked rows can't have them.
    franchise = required_keys_for_shape("franchise")
    assert "resource_collection" in franchise
    assert "year" not in franchise and "is_anime" not in franchise
    assert "title_cn" in franchise and "is_batch" in franchise
    movie = required_keys_for_shape("movie")
    assert BASE_REQUIRED_FIELDS <= movie
    assert not ({"season", "episode"} & movie)


def test_sections_order_work_type_first_then_semantic():
    assert REQUIRED_FIELD_SECTIONS == ("base", "tv", "pack", "release", "work")
    sections = {entry["section"] for entry in REQUIRED_FIELD_CATALOG.values()}
    assert sections == set(REQUIRED_FIELD_SECTIONS)


def test_groups_cover_all_catalog_entries():
    grouped = {entry["group"] for entry in REQUIRED_FIELD_CATALOG.values()}
    assert grouped == set(REQUIRED_FIELD_GROUPS)


def test_applies_to_mirrors_dsl_namespacing():
    """Work-pair keys require a linked work; resource-level parse fields don't."""
    for key, entry in REQUIRED_FIELD_CATALOG.items():
        namespaced = any(f.startswith(("series.", "movie.")) for f in entry["dsl_fields"])
        if namespaced:
            assert entry.get("applies_to"), key
            assert set(entry["applies_to"]) <= {"tv_single", "tv_batch", "movie"}


# ---------------------------------------------------------------------------
# validate_required_fields
# ---------------------------------------------------------------------------


def test_validate_required_fields_accepts_catalog_keys():
    assert validate_required_fields(None) == []
    assert validate_required_fields([]) == []
    assert validate_required_fields(list(REQUIRED_FIELD_CATALOG)) == []


def test_validate_required_fields_rejects_unknown():
    errs = validate_required_fields(["rating", "bogus"])
    assert len(errs) == 1
    assert "bogus" in errs[0]


# ---------------------------------------------------------------------------
# normalize_required_fields
# ---------------------------------------------------------------------------


def test_normalize_unions_locked_baseline():
    out = normalize_required_fields(["rating"])
    assert set(out) == LOCKED_REQUIRED_FIELDS | {"rating"}


def test_normalize_orders_canonically_and_dedupes():
    out = normalize_required_fields(["year", "rating", "title_cn", "title_cn"])
    expected_order = [
        k for k in REQUIRED_FIELD_CATALOG
        if k in {"year", "rating", "title_cn"} | set(LOCKED_REQUIRED_FIELDS)
    ]
    assert out == expected_order


def test_normalize_empty_gives_locked_baseline():
    assert normalize_required_fields([]) == [
        k for k in REQUIRED_FIELD_CATALOG if k in LOCKED_REQUIRED_FIELDS
    ]
    assert normalize_required_fields(None) == normalize_required_fields([])


def test_normalize_includes_year_is_anime_and_tv_machinery():
    baseline = normalize_required_fields([])
    assert {"year", "is_anime", "season", "episode",
            "episode_start", "episode_end", "resource_collection"} <= set(baseline)


def test_normalize_drops_unknown_keys_defensively():
    out = normalize_required_fields(["bogus", "rating"])
    assert "bogus" not in out
    assert "rating" in out


# ---------------------------------------------------------------------------
# allowed_agent_filter_fields
# ---------------------------------------------------------------------------


def test_allowed_fields_none_is_legacy_fallback():
    # Post-migration rows are always lists; None remains a defensive fallback
    # meaning unrestricted (legacy behaviour).
    assert allowed_agent_filter_fields(None) is None


def test_allowed_fields_baseline_unlocks_base_work_pairs():
    allowed = allowed_agent_filter_fields(normalize_required_fields([]))
    assert allowed is not None
    # Every resource-level field is allowed…
    assert "episode" in allowed
    assert "subtitle_group" in allowed
    assert "content_type" in allowed
    assert "collection" in allowed  # resource-level franchise link
    # …and the base tier unlocks its work-pair fields on every channel…
    assert {"series.year", "movie.year", "series.is_anime", "movie.is_anime"} <= allowed
    # …while opt-in work pairs stay gated.
    assert "series.rating" not in allowed
    assert "series.genre" not in allowed
    assert "series.collection" not in allowed


def test_allowed_fields_maps_declared_keys():
    allowed = allowed_agent_filter_fields(["rating", "genre"])
    assert allowed is not None
    assert {"series.rating", "movie.rating", "series.genre", "movie.genre"} <= allowed
    assert "series.collection" not in allowed
    assert "episode" in allowed


def test_allowed_fields_ignores_unknown_keys_defensively():
    # Unknown keys should have been rejected at the schema layer; the gate
    # must not crash on legacy/bad rows.
    allowed = allowed_agent_filter_fields(["nope"])
    assert allowed is not None
    assert "series.rating" not in allowed


def test_allowed_fields_covers_all_resource_level_fields():
    allowed = allowed_agent_filter_fields([])
    assert allowed is not None
    resource_level = {f for f in ALL_FIELDS if not f.startswith(("series.", "movie."))}
    assert resource_level <= allowed


# ---------------------------------------------------------------------------
# validate_filter_against_allowed
# ---------------------------------------------------------------------------


def test_filter_gating_walks_nested_trees():
    cfg = {
        "combinator": "and",
        "conditions": [
            {"field": "resolution", "operator": "eq", "value": "1080p"},
            {
                "combinator": "or",
                "conditions": [
                    {"field": "series.rating", "operator": "gte", "value": 8},
                    {"field": "series.collection", "operator": "contains", "value": "x"},
                ],
            },
        ],
    }
    allowed = allowed_agent_filter_fields(["rating"])
    assert allowed is not None
    errs = validate_filter_against_allowed(cfg, allowed)
    assert errs == ["field 'series.collection' is not in the channel's required metadata fields"]


def test_filter_gating_passes_when_all_allowed():
    cfg = {
        "combinator": "and",
        "conditions": [
            {"field": "series.rating", "operator": "gte", "value": 8},
            {"field": "movie.is_anime", "operator": "eq", "value": True},
            {"field": "series.year", "operator": "gte", "value": 2020},
        ],
    }
    # Channel values are always stored normalized — the baseline (incl. the
    # year/is_anime work pairs) rides along with any explicit declaration.
    allowed = allowed_agent_filter_fields(normalize_required_fields(["rating"]))
    assert allowed is not None
    assert validate_filter_against_allowed(cfg, allowed) == []
