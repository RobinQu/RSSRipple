"""Tests for the Filter DSL engine (evaluate_filter_config/validate/merge)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.filter_engine import (
    evaluate_field_condition,
    evaluate_filter_config,
    get_field_value,
    merge_filters,
    validate_filter_config,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _res(**overrides):
    """Build a FileResource-like object with default values."""
    defaults = dict(
        subtitle_group="LoliHouse",
        resolution="1080p",
        source="WebRip",
        video_codec="HEVC",
        audio_codec="AAC",
        subtitle_type="CHS",
        container="MKV",
        title_cn="标题",
        title_en="Title",
        search_title="Title 标题",
        file_size=1_500_000_000,
        episode=3,
        season=1,
        is_batch=False,
        episode_start=None,
        episode_end=None,
        subtitle_langs=None,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


# ---------------------------------------------------------------------------
# validate_filter_config
# ---------------------------------------------------------------------------


class TestValidateFilterConfig:
    def test_none_is_valid(self):
        assert validate_filter_config(None) == []

    def test_empty_dict_is_valid_as_passthrough(self):
        # Empty dict is not valid per our validator (conditions list required).
        # But evaluate_filter_config treats empty as pass-all.
        errs = validate_filter_config({})
        # {} is neither a valid bool nor a field condition
        assert any("must be a" in e for e in errs)

    def test_valid_simple_and(self):
        cfg = {
            "combinator": "and",
            "conditions": [
                {"field": "resolution", "operator": "eq", "value": "1080p"},
            ],
        }
        assert validate_filter_config(cfg) == []

    def test_unknown_field(self):
        cfg = {"combinator": "and", "conditions": [
            {"field": "bogus", "operator": "eq", "value": "x"}
        ]}
        errs = validate_filter_config(cfg)
        assert any("unknown field" in e for e in errs)

    def test_unknown_operator(self):
        cfg = {"combinator": "and", "conditions": [
            {"field": "resolution", "operator": "starts_with", "value": "1"}
        ]}
        errs = validate_filter_config(cfg)
        assert any("unknown operator" in e for e in errs)

    def test_string_field_rejects_numeric_operators(self):
        cfg = {"combinator": "and", "conditions": [
            {"field": "resolution", "operator": "gt", "value": "1080"}
        ]}
        errs = validate_filter_config(cfg)
        assert any("not supported for string field" in e for e in errs)

    def test_number_field_rejects_string_operators(self):
        cfg = {"combinator": "and", "conditions": [
            {"field": "episode", "operator": "contains", "value": "3"}
        ]}
        errs = validate_filter_config(cfg)
        assert any("not supported for number field" in e for e in errs)

    def test_in_operator_requires_nonempty_list(self):
        cfg = {"combinator": "and", "conditions": [
            {"field": "resolution", "operator": "in", "value": []}
        ]}
        errs = validate_filter_config(cfg)
        assert any("'in' requires a non-empty list" in e for e in errs)

    def test_in_accepts_comma_string(self):
        cfg = {"combinator": "and", "conditions": [
            {"field": "resolution", "operator": "in", "value": "1080p,2160p"}
        ]}
        assert validate_filter_config(cfg) == []

    def test_in_operator_rejects_non_list_non_string(self):
        cfg = {"combinator": "and", "conditions": [
            {"field": "resolution", "operator": "in", "value": 123}
        ]}
        errs = validate_filter_config(cfg)
        assert any("requires a list or comma-separated string" in e for e in errs)

    def test_regex_invalid(self):
        cfg = {"combinator": "and", "conditions": [
            {"field": "title_en", "operator": "regex", "value": "(unclosed"}
        ]}
        errs = validate_filter_config(cfg)
        assert any("invalid regex" in e for e in errs)

    def test_regex_valid(self):
        cfg = {"combinator": "and", "conditions": [
            {"field": "title_en", "operator": "regex", "value": r"^Title.*"}
        ]}
        assert validate_filter_config(cfg) == []

    def test_bad_combinator(self):
        cfg = {"combinator": "xor", "conditions": [
            {"field": "resolution", "operator": "eq", "value": "1080p"}
        ]}
        errs = validate_filter_config(cfg)
        assert any("must be 'and' or 'or'" in e for e in errs)

    def test_empty_conditions(self):
        cfg = {"combinator": "and", "conditions": []}
        errs = validate_filter_config(cfg)
        assert any("non-empty list" in e for e in errs)

    def test_nested_groups_valid(self):
        cfg = {
            "combinator": "or",
            "conditions": [
                {"combinator": "and", "conditions": [
                    {"field": "resolution", "operator": "eq", "value": "2160p"},
                    {"field": "container", "operator": "eq", "value": "MKV"},
                ]},
                {"field": "subtitle_group", "operator": "eq", "value": "Official"},
            ],
        }
        assert validate_filter_config(cfg) == []

    def test_is_not_must_be_bool(self):
        cfg = {"combinator": "and", "is_not": "no", "conditions": [
            {"field": "resolution", "operator": "eq", "value": "720p"}
        ]}
        errs = validate_filter_config(cfg)
        assert any("is_not" in e and "bool" in e for e in errs)

    def test_non_dict_node(self):
        cfg = {"combinator": "and", "conditions": ["bad"]}
        errs = validate_filter_config(cfg)
        assert any("must be a dict" in e for e in errs)


# ---------------------------------------------------------------------------
# Bool combinator evaluation
# ---------------------------------------------------------------------------


class TestBoolCombinators:
    def test_none_passes(self):
        assert evaluate_filter_config(None, _res()) is True

    def test_empty_dict_passes(self):
        assert evaluate_filter_config({}, _res()) is True

    def test_and_all_true(self):
        cfg = {
            "combinator": "and",
            "conditions": [
                {"field": "resolution", "operator": "eq", "value": "1080p"},
                {"field": "container", "operator": "eq", "value": "mkv"},
            ],
        }
        assert evaluate_filter_config(cfg, _res()) is True

    def test_and_one_false(self):
        cfg = {
            "combinator": "and",
            "conditions": [
                {"field": "resolution", "operator": "eq", "value": "2160p"},
                {"field": "container", "operator": "eq", "value": "MKV"},
            ],
        }
        assert evaluate_filter_config(cfg, _res()) is False

    def test_or_any_true(self):
        cfg = {
            "combinator": "or",
            "conditions": [
                {"field": "resolution", "operator": "eq", "value": "2160p"},
                {"field": "container", "operator": "eq", "value": "MKV"},
            ],
        }
        assert evaluate_filter_config(cfg, _res()) is True

    def test_or_all_false(self):
        cfg = {
            "combinator": "or",
            "conditions": [
                {"field": "resolution", "operator": "eq", "value": "2160p"},
                {"field": "container", "operator": "eq", "value": "MP4"},
            ],
        }
        assert evaluate_filter_config(cfg, _res()) is False

    def test_nested_groups(self):
        cfg = {
            "combinator": "or",
            "conditions": [
                {"combinator": "and", "conditions": [
                    {"field": "resolution", "operator": "eq", "value": "2160p"},
                    {"field": "container", "operator": "eq", "value": "MP4"},
                ]},
                {"combinator": "and", "conditions": [
                    {"field": "resolution", "operator": "eq", "value": "1080p"},
                    {"field": "container", "operator": "eq", "value": "MKV"},
                ]},
            ],
        }
        assert evaluate_filter_config(cfg, _res()) is True

    def test_is_not_negates(self):
        cfg = {
            "combinator": "and",
            "is_not": True,
            "conditions": [
                {"field": "resolution", "operator": "eq", "value": "1080p"},
            ],
        }
        assert evaluate_filter_config(cfg, _res()) is False

    def test_invalid_node_returns_false(self):
        assert evaluate_filter_config({"foo": "bar"}, _res()) is False


# ---------------------------------------------------------------------------
# String operators
# ---------------------------------------------------------------------------


class TestStringOperators:
    def test_eq_case_insensitive(self):
        r = _res(resolution="1080P")
        assert evaluate_field_condition(
            {"field": "resolution", "operator": "eq", "value": "1080p"}, r
        ) is True

    def test_eq_strips_spaces(self):
        r = _res(subtitle_group="  LoliHouse  ")
        # attribute doesn't have spaces, but value does
        assert evaluate_field_condition(
            {"field": "subtitle_group", "operator": "eq", "value": "  LoliHouse "}, r
        ) is True

    def test_ne(self):
        assert evaluate_field_condition(
            {"field": "resolution", "operator": "ne", "value": "2160p"}, _res()
        ) is True
        assert evaluate_field_condition(
            {"field": "resolution", "operator": "ne", "value": "1080p"}, _res()
        ) is False

    def test_contains(self):
        assert evaluate_field_condition(
            {"field": "subtitle_group", "operator": "contains", "value": "loli"},
            _res(),
        ) is True
        assert evaluate_field_condition(
            {"field": "subtitle_group", "operator": "contains", "value": "zzz"},
            _res(),
        ) is False

    def test_fuzzy(self):
        # close enough
        assert evaluate_field_condition(
            {"field": "title_en", "operator": "fuzzy", "value": "Titel"}, _res()
        ) is True
        # totally different
        assert evaluate_field_condition(
            {"field": "title_en", "operator": "fuzzy", "value": "xyzqwert"}, _res()
        ) is False

    def test_in_list(self):
        r = _res(resolution="1080p")
        assert evaluate_field_condition(
            {"field": "resolution", "operator": "in", "value": ["1080p", "2160p"]}, r
        ) is True
        assert evaluate_field_condition(
            {"field": "resolution", "operator": "in", "value": ["720p", "480p"]}, r
        ) is False

    def test_in_comma_string(self):
        assert evaluate_field_condition(
            {"field": "resolution", "operator": "in", "value": "1080p, 2160p"},
            _res(),
        ) is True

    def test_in_substring_contains(self):
        # "in" uses substring matching per spec
        assert evaluate_field_condition(
            {"field": "subtitle_group", "operator": "in", "value": ["Loli"]},
            _res(),
        ) is True

    def test_regex_match(self):
        assert evaluate_field_condition(
            {"field": "title_en", "operator": "regex", "value": r"^Titl"}, _res()
        ) is True

    def test_regex_no_match(self):
        assert evaluate_field_condition(
            {"field": "title_en", "operator": "regex", "value": r"^XYZ"}, _res()
        ) is False

    def test_regex_bad_returns_false(self):
        assert evaluate_field_condition(
            {"field": "title_en", "operator": "regex", "value": "(unclosed"}, _res()
        ) is False


# ---------------------------------------------------------------------------
# Numeric operators
# ---------------------------------------------------------------------------


class TestNumericOperators:
    def test_eq(self):
        assert evaluate_field_condition(
            {"field": "episode", "operator": "eq", "value": 3}, _res()
        ) is True

    def test_ne(self):
        assert evaluate_field_condition(
            {"field": "episode", "operator": "ne", "value": 5}, _res()
        ) is True

    def test_gt_gte_lt_lte(self):
        r = _res(file_size=1_500_000_000)
        assert evaluate_field_condition(
            {"field": "file_size", "operator": "gt", "value": 1_000_000_000}, r
        ) is True
        assert evaluate_field_condition(
            {"field": "file_size", "operator": "gte", "value": 1_500_000_000}, r
        ) is True
        assert evaluate_field_condition(
            {"field": "file_size", "operator": "lt", "value": 2_000_000_000}, r
        ) is True
        assert evaluate_field_condition(
            {"field": "file_size", "operator": "lte", "value": 1_500_000_000}, r
        ) is True
        assert evaluate_field_condition(
            {"field": "file_size", "operator": "gt", "value": 2_000_000_000}, r
        ) is False

    def test_in_list_numbers(self):
        assert evaluate_field_condition(
            {"field": "episode", "operator": "in", "value": [3, 4, 5]}, _res()
        ) is True
        assert evaluate_field_condition(
            {"field": "episode", "operator": "in", "value": [1, 2]}, _res()
        ) is False

    def test_numeric_value_non_numeric_returns_false(self):
        assert evaluate_field_condition(
            {"field": "episode", "operator": "eq", "value": "abc"}, _res()
        ) is False


# ---------------------------------------------------------------------------
# Empty / None handling
# ---------------------------------------------------------------------------


class TestEmptyHandling:
    def test_none_string_positive_ops_fail(self):
        r = _res(subtitle_group=None)
        for op in ("eq", "contains", "fuzzy", "in", "regex"):
            assert evaluate_field_condition(
                {"field": "subtitle_group", "operator": op, "value": "x"}, r
            ) is False

    def test_empty_string_positive_ops_fail(self):
        r = _res(subtitle_group="   ")
        for op in ("eq", "contains", "fuzzy", "regex"):
            assert evaluate_field_condition(
                {"field": "subtitle_group", "operator": op, "value": "x"}, r
            ) is False

    def test_none_string_ne_passes(self):
        r = _res(subtitle_group=None)
        assert evaluate_field_condition(
            {"field": "subtitle_group", "operator": "ne", "value": "x"}, r
        ) is True

    def test_none_number_positive_ops_fail(self):
        r = _res(episode=None)
        for op in ("eq", "gt", "gte", "lt", "lte"):
            assert evaluate_field_condition(
                {"field": "episode", "operator": op, "value": 1}, r
            ) is False

    def test_none_number_ne_passes(self):
        r = _res(episode=None)
        assert evaluate_field_condition(
            {"field": "episode", "operator": "ne", "value": 1}, r
        ) is True

    def test_nonnumeric_raw_for_numeric_field_ne_passes(self):
        r = SimpleNamespace(episode="not-a-number", **{k: getattr(_res(), k) for k in ("resolution", "subtitle_group", "container", "file_size", "season")})
        # Actually let's just build a minimal namespace:
        r = SimpleNamespace(episode="not-a-number")
        assert evaluate_field_condition(
            {"field": "episode", "operator": "ne", "value": 1}, r
        ) is True
        assert evaluate_field_condition(
            {"field": "episode", "operator": "eq", "value": 1}, r
        ) is False


# ---------------------------------------------------------------------------
# get_field_value / unknown field
# ---------------------------------------------------------------------------


def test_get_field_value_returns_none_for_missing():
    r = _res()
    assert get_field_value(r, "nonexistent") is None
    assert evaluate_field_condition(
        {"field": "nonexistent", "operator": "eq", "value": "x"}, r
    ) is False


# ---------------------------------------------------------------------------
# merge_filters
# ---------------------------------------------------------------------------


class TestMergeFilters:
    def test_both_none(self):
        assert merge_filters(None, None) is None

    def test_only_global(self):
        # Non-empty filter is kept
        g = {"combinator": "and", "conditions": [
            {"field": "resolution", "operator": "eq", "value": "1080p"}]}
        assert merge_filters(g, None) == g
        # Empty dict is treated as null
        assert merge_filters({}, None) is None

    def test_only_override(self):
        o = {"combinator": "and", "conditions": [
            {"field": "container", "operator": "eq", "value": "MKV"}]}
        assert merge_filters(None, o) == o

    def test_both_present_wraps_in_and(self):
        g = {"combinator": "and", "conditions": [{"field": "a", "operator": "eq", "value": "1"}]}
        o = {"combinator": "and", "conditions": [{"field": "b", "operator": "eq", "value": "2"}]}
        merged = merge_filters(g, o)
        assert merged == {"combinator": "and", "conditions": [g, o]}


# ---------------------------------------------------------------------------
# Batch (合集) fields — is_batch bool + episode_start/end numbers
# ---------------------------------------------------------------------------


class TestBatchFields:
    def test_is_batch_eq_true(self):
        cond = {"field": "is_batch", "operator": "eq", "value": True}
        assert evaluate_field_condition(cond, _res(is_batch=True)) is True
        assert evaluate_field_condition(cond, _res(is_batch=False)) is False

    def test_is_batch_eq_false_excludes_batches(self):
        cond = {"field": "is_batch", "operator": "eq", "value": False}
        assert evaluate_field_condition(cond, _res(is_batch=True)) is False
        assert evaluate_field_condition(cond, _res(is_batch=False)) is True

    def test_is_batch_ne(self):
        cond = {"field": "is_batch", "operator": "ne", "value": True}
        assert evaluate_field_condition(cond, _res(is_batch=False)) is True
        assert evaluate_field_condition(cond, _res(is_batch=True)) is False

    def test_is_batch_accepts_string_and_int(self):
        assert evaluate_field_condition(
            {"field": "is_batch", "operator": "eq", "value": "true"},
            _res(is_batch=True),
        ) is True
        assert evaluate_field_condition(
            {"field": "is_batch", "operator": "eq", "value": 1},
            _res(is_batch=True),
        ) is True
        assert evaluate_field_condition(
            {"field": "is_batch", "operator": "eq", "value": "no"},
            _res(is_batch=False),
        ) is True

    def test_is_batch_missing_field_is_false(self):
        r = _res()
        del r.is_batch  # simulate an older row without the column value
        assert evaluate_field_condition(
            {"field": "is_batch", "operator": "eq", "value": False}, r
        ) is True

    def test_episode_start_gte(self):
        cond = {"field": "episode_start", "operator": "gte", "value": 1}
        assert evaluate_field_condition(cond, _res(is_batch=True, episode_start=1, episode_end=13)) is True
        assert evaluate_field_condition(cond, _res(is_batch=True, episode_start=None)) is False

    def test_episode_end_lte(self):
        cond = {"field": "episode_end", "operator": "lte", "value": 12}
        assert evaluate_field_condition(cond, _res(is_batch=True, episode_start=1, episode_end=12)) is True
        assert evaluate_field_condition(cond, _res(is_batch=True, episode_start=1, episode_end=24)) is False


class TestBatchValidation:
    def test_is_batch_only_bool_ops(self):
        errors = validate_filter_config(
            {"combinator": "and", "conditions": [
                {"field": "is_batch", "operator": "contains", "value": True}
            ]}
        )
        assert errors  # "contains" is not a bool op

    def test_is_batch_valid(self):
        errors = validate_filter_config(
            {"combinator": "and", "conditions": [
                {"field": "is_batch", "operator": "eq", "value": True}
            ]}
        )
        assert not errors


# ---------------------------------------------------------------------------
# List-of-strings field — subtitle_langs
# ---------------------------------------------------------------------------


class TestSubtitleLangs:
    def test_contains_matches_any_element(self):
        cond = {"field": "subtitle_langs", "operator": "contains", "value": "zh-CN"}
        assert evaluate_field_condition(cond, _res(subtitle_langs=["zh-CN", "zh-TW"])) is True
        assert evaluate_field_condition(cond, _res(subtitle_langs=["ja"])) is False

    def test_contains_is_case_insensitive(self):
        cond = {"field": "subtitle_langs", "operator": "contains", "value": "ZH-CN"}
        assert evaluate_field_condition(cond, _res(subtitle_langs=["zh-CN"])) is True

    def test_in_matches_any_value(self):
        cond = {"field": "subtitle_langs", "operator": "in", "value": ["ja", "en"]}
        assert evaluate_field_condition(cond, _res(subtitle_langs=["zh-CN", "en"])) is True
        assert evaluate_field_condition(cond, _res(subtitle_langs=["zh-CN"])) is False

    def test_eq_requires_exact_set(self):
        cond = {"field": "subtitle_langs", "operator": "eq", "value": ["zh-CN", "zh-TW"]}
        assert evaluate_field_condition(cond, _res(subtitle_langs=["zh-CN", "zh-TW"])) is True
        # Ordering doesn't matter for set equality
        assert evaluate_field_condition(cond, _res(subtitle_langs=["zh-TW", "zh-CN"])) is True
        assert evaluate_field_condition(cond, _res(subtitle_langs=["zh-CN"])) is False

    def test_ne(self):
        cond = {"field": "subtitle_langs", "operator": "ne", "value": ["zh-CN"]}
        assert evaluate_field_condition(cond, _res(subtitle_langs=["ja"])) is True
        assert evaluate_field_condition(cond, _res(subtitle_langs=["zh-CN"])) is False

    def test_none_treated_as_empty(self):
        # Legacy row with no parsing pass yet
        cond = {"field": "subtitle_langs", "operator": "contains", "value": "zh-CN"}
        assert evaluate_field_condition(cond, _res(subtitle_langs=None)) is False
        # `ne` against an expected set passes when the row is empty
        assert evaluate_field_condition(
            {"field": "subtitle_langs", "operator": "ne", "value": ["zh-CN"]},
            _res(subtitle_langs=None),
        ) is True

    def test_validation_rejects_unsupported_ops(self):
        errors = validate_filter_config(
            {"combinator": "and", "conditions": [
                {"field": "subtitle_langs", "operator": "regex", "value": "zh"}
            ]}
        )
        assert errors

    def test_validation_accepts_supported_ops(self):
        for op, val in [
            ("contains", "zh-CN"),
            ("in", ["zh-CN", "zh-TW"]),
            ("eq", ["zh-CN"]),
            ("ne", ["zh-CN"]),
        ]:
            errors = validate_filter_config(
                {"combinator": "and", "conditions": [
                    {"field": "subtitle_langs", "operator": op, "value": val}
                ]}
            )
            assert not errors, f"op={op} should be valid"
