"""In-process coverage for Filter DSL engine branches the HTTP suites miss.

Targets ``app/services/filter_engine.py``:

- ``_coerce_bool`` numeric/string/fallback coercions.
- ``validate_field_conditions`` (Agent pick-preferences validator).
- ``_validate_node`` structural errors (non-dict child, bad combinator,
  empty conditions, non-bool is_not, per-type operator gating, ``in`` /
  ``regex`` / numeric value validation).
- ``evaluate_filter_config`` pass-all/invalid-node edges.
- List-field edges: scalar ``eq``/``ne`` coercion, ``subtitle_groups``
  fuzzy/regex, unsupported-operator fallthrough.
- Bool tri-state null semantics and unsupported-operator fallthrough.
- Number-field ``in`` coercion, non-numeric compare values, ``ne``.
- String-field non-string expected values, bad regex, unknown operator.
- ``get_field_value``: audio ``content_type``, resource-level ``collection``
  and ``series.collection`` display-name resolution.

Resources are ``SimpleNamespace`` stand-ins; the engine is deliberately
ORM-agnostic (``loaded_relation`` reads ``__dict__`` only).
"""

from __future__ import annotations

from types import SimpleNamespace

from app.services.filter_engine import (
    _coerce_bool,
    evaluate_field_condition,
    evaluate_filter_config,
    get_field_value,
    validate_field_conditions,
    validate_filter_config,
)


def _res(**overrides):
    defaults = dict(
        subtitle_group="LoliHouse",
        resolution="1080p",
        source="WebRip",
        title_en="Title",
        file_size=1_500_000_000,
        episode=3,
        season=1,
        is_batch=False,
        episode_start=None,
        episode_end=None,
        absolute_episode=None,
        subtitle_langs=None,
        episode_confidence=None,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


# ---------------------------------------------------------------------------
# _coerce_bool
# ---------------------------------------------------------------------------


class TestCoerceBool:
    def test_numeric_values(self):
        assert _coerce_bool(2) is True
        assert _coerce_bool(0.0) is False

    def test_string_forms_case_insensitive(self):
        assert _coerce_bool("TRUE") is True
        assert _coerce_bool(" y ") is True
        assert _coerce_bool("Off") is False
        assert _coerce_bool("n") is False

    def test_fallback_to_truthiness(self):
        assert _coerce_bool("maybe") is True
        assert _coerce_bool([]) is False


# ---------------------------------------------------------------------------
# validate_field_conditions (flat FieldCondition list, e.g. pick preferences)
# ---------------------------------------------------------------------------


class TestValidateFieldConditions:
    def test_none_is_valid(self):
        assert validate_field_conditions(None) == []

    def test_non_list_rejected(self):
        errs = validate_field_conditions({"field": "resolution"})
        assert any("must be a list" in e for e in errs)

    def test_element_missing_operator_rejected(self):
        errs = validate_field_conditions([{"field": "resolution"}])
        assert any("$[0]" in e and "FieldCondition" in e for e in errs)

    def test_value_less_operator_without_value_key_is_valid(self):
        assert validate_field_conditions(
            [{"field": "resolution", "operator": "is_empty"}]
        ) == []

    def test_element_validation_errors_are_path_prefixed(self):
        errs = validate_field_conditions(
            [{"field": "bogus", "operator": "eq", "value": "x"}]
        )
        assert any("$[0].field" in e and "unknown field" in e for e in errs)


# ---------------------------------------------------------------------------
# _validate_node structural branches
# ---------------------------------------------------------------------------


class TestValidateNodeEdges:
    def test_non_dict_child(self):
        errs = validate_filter_config({"combinator": "and", "conditions": ["bad"]})
        assert any("must be a dict" in e for e in errs)

    def test_bad_combinator(self):
        errs = validate_filter_config({"combinator": "xor", "conditions": [
            {"field": "resolution", "operator": "eq", "value": "1080p"}]})
        assert any("must be 'and' or 'or'" in e for e in errs)

    def test_empty_conditions_list(self):
        errs = validate_filter_config({"combinator": "and", "conditions": []})
        assert any("non-empty list" in e for e in errs)

    def test_is_not_must_be_bool(self):
        errs = validate_filter_config({"combinator": "and", "is_not": "yes", "conditions": [
            {"field": "resolution", "operator": "eq", "value": "1080p"}]})
        assert any("is_not" in e and "bool" in e for e in errs)

    def test_number_op_on_string_field(self):
        errs = validate_filter_config({"combinator": "and", "conditions": [
            {"field": "resolution", "operator": "gte", "value": 1080}]})
        assert any("not supported for string field" in e for e in errs)

    def test_string_op_on_bool_field(self):
        errs = validate_filter_config({"combinator": "and", "conditions": [
            {"field": "series.is_anime", "operator": "contains", "value": "x"}]})
        assert any("not supported for bool field" in e for e in errs)

    def test_in_comma_string_of_only_separators(self):
        errs = validate_filter_config({"combinator": "and", "conditions": [
            {"field": "resolution", "operator": "in", "value": ", , ,"}]})
        assert any("'in' requires a non-empty list" in e for e in errs)

    def test_in_rejects_scalar_value(self):
        errs = validate_filter_config({"combinator": "and", "conditions": [
            {"field": "resolution", "operator": "in", "value": 1080}]})
        assert any("requires a list or comma-separated string" in e for e in errs)

    def test_regex_requires_string_value(self):
        errs = validate_filter_config({"combinator": "and", "conditions": [
            {"field": "title_en", "operator": "regex", "value": 42}]})
        assert any("'regex' requires a non-empty string" in e for e in errs)

    def test_numeric_field_rejects_non_numeric_value(self):
        errs = validate_filter_config({"combinator": "and", "conditions": [
            {"field": "episode", "operator": "gte", "value": "abc"}]})
        assert any("numeric field requires a numeric value" in e for e in errs)

    def test_numeric_field_accepts_numeric_string(self):
        assert validate_filter_config({"combinator": "and", "conditions": [
            {"field": "episode", "operator": "gte", "value": "3.5"}]}) == []


# ---------------------------------------------------------------------------
# evaluate_filter_config top-level edges
# ---------------------------------------------------------------------------


class TestEvaluateTopLevel:
    def test_none_and_empty_config_pass_all(self):
        assert evaluate_filter_config(None, _res()) is True
        assert evaluate_filter_config({}, _res()) is True

    def test_unrecognized_node_fails_closed(self):
        assert evaluate_filter_config({"foo": "bar"}, _res()) is False

    def test_validate_unrecognized_node(self):
        errs = validate_filter_config({"foo": "bar"})
        assert any("must be a BoolCondition or FieldCondition" in e for e in errs)


# ---------------------------------------------------------------------------
# List-of-strings field edges
# ---------------------------------------------------------------------------


class TestListFieldEdges:
    def test_eq_accepts_scalar_expected(self):
        r = _res(subtitle_langs=["zh-CN"])
        assert evaluate_field_condition(
            {"field": "subtitle_langs", "operator": "eq", "value": "zh-cn"}, r
        ) is True
        assert evaluate_field_condition(
            {"field": "subtitle_langs", "operator": "eq", "value": "ja"}, r
        ) is False

    def test_eq_blank_expected_matches_only_empty(self):
        cond = {"field": "subtitle_langs", "operator": "eq", "value": ""}
        assert evaluate_field_condition(cond, _res(subtitle_langs=None)) is True
        assert evaluate_field_condition(cond, _res(subtitle_langs=["zh-CN"])) is False

    def test_ne_with_list_and_scalar_expected(self):
        r = _res(subtitle_langs=["zh-CN", "en"])
        assert evaluate_field_condition(
            {"field": "subtitle_langs", "operator": "ne", "value": ["zh-cn", "en"]}, r
        ) is False
        assert evaluate_field_condition(
            {"field": "subtitle_langs", "operator": "ne", "value": ["zh-cn"]}, r
        ) is True
        assert evaluate_field_condition(
            {"field": "subtitle_langs", "operator": "ne", "value": "zh-cn"}, r
        ) is True

    def test_subtitle_groups_fuzzy(self):
        r = _res(subtitle_groups=["LoliHouse"])
        assert evaluate_field_condition(
            {"field": "subtitle_groups", "operator": "fuzzy", "value": "lolihouse"}, r
        ) is True
        assert evaluate_field_condition(
            {"field": "subtitle_groups", "operator": "fuzzy", "value": "zzzqwer"}, r
        ) is False

    def test_subtitle_groups_regex(self):
        r = _res(subtitle_groups=["LoliHouse"])
        assert evaluate_field_condition(
            {"field": "subtitle_groups", "operator": "regex", "value": r"^loli"}, r
        ) is True
        assert evaluate_field_condition(
            {"field": "subtitle_groups", "operator": "regex", "value": r"^ani"}, r
        ) is False

    def test_subtitle_groups_bad_regex_returns_false(self):
        r = _res(subtitle_groups=["LoliHouse"])
        assert evaluate_field_condition(
            {"field": "subtitle_groups", "operator": "regex", "value": "(unclosed"}, r
        ) is False

    def test_unsupported_list_op_falls_through_to_false(self):
        # ``regex``/``fuzzy`` are subtitle_groups-only; on other list fields
        # (or any unknown op) evaluation fails closed. Validation would
        # reject these at save time, but the evaluator stays defensive.
        r = _res(subtitle_langs=["zh-CN"])
        assert evaluate_field_condition(
            {"field": "subtitle_langs", "operator": "regex", "value": "zh"}, r
        ) is False
        assert evaluate_field_condition(
            {"field": "subtitle_langs", "operator": "fuzzy", "value": "zh"}, r
        ) is False


# ---------------------------------------------------------------------------
# Bool field edges (tri-state series.is_anime / movie.is_anime)
# ---------------------------------------------------------------------------


class TestBoolFieldEdges:
    def test_null_work_bool_positive_ops_fail_ne_passes(self):
        r = _res(series=SimpleNamespace(is_anime=None))
        assert evaluate_field_condition(
            {"field": "series.is_anime", "operator": "eq", "value": True}, r
        ) is False
        assert evaluate_field_condition(
            {"field": "series.is_anime", "operator": "ne", "value": True}, r
        ) is True

    def test_false_work_bool_against_string_expected(self):
        r = _res(movie=SimpleNamespace(is_anime=False))
        assert evaluate_field_condition(
            {"field": "movie.is_anime", "operator": "eq", "value": "true"}, r
        ) is False
        assert evaluate_field_condition(
            {"field": "movie.is_anime", "operator": "ne", "value": "true"}, r
        ) is True

    def test_unsupported_bool_op_fails_closed(self):
        r = _res(is_batch=True)
        assert evaluate_field_condition(
            {"field": "is_batch", "operator": "gt", "value": True}, r
        ) is False


# ---------------------------------------------------------------------------
# Number field edges
# ---------------------------------------------------------------------------


class TestNumberFieldEdges:
    def test_none_raw_ne_passes_positive_ops_fail(self):
        r = _res(absolute_episode=None)
        assert evaluate_field_condition(
            {"field": "absolute_episode", "operator": "eq", "value": 30}, r
        ) is False
        assert evaluate_field_condition(
            {"field": "absolute_episode", "operator": "ne", "value": 30}, r
        ) is True

    def test_non_numeric_raw_only_ne_passes(self):
        r = _res(absolute_episode="not-a-number")
        assert evaluate_field_condition(
            {"field": "absolute_episode", "operator": "eq", "value": 30}, r
        ) is False
        assert evaluate_field_condition(
            {"field": "absolute_episode", "operator": "ne", "value": 30}, r
        ) is True

    def test_in_skips_unparsable_members(self):
        r = _res(absolute_episode=30)
        assert evaluate_field_condition(
            {"field": "absolute_episode", "operator": "in", "value": ["bad", "30"]}, r
        ) is True
        assert evaluate_field_condition(
            {"field": "absolute_episode", "operator": "in", "value": ["bad", "oops"]}, r
        ) is False

    def test_in_with_scalar_expected_coerced_to_list(self):
        r = _res(episode=7)
        assert evaluate_field_condition(
            {"field": "episode", "operator": "in", "value": 7}, r
        ) is True
        assert evaluate_field_condition(
            {"field": "episode", "operator": "in", "value": 8}, r
        ) is False

    def test_non_numeric_compare_value_fails_closed(self):
        assert evaluate_field_condition(
            {"field": "file_size", "operator": "gte", "value": "huge"}, _res()
        ) is False

    def test_ne_compares_numeric(self):
        assert evaluate_field_condition(
            {"field": "episode", "operator": "ne", "value": 3}, _res()
        ) is False
        assert evaluate_field_condition(
            {"field": "episode", "operator": "ne", "value": 4}, _res()
        ) is True

    def test_unknown_number_op_fails_closed(self):
        assert evaluate_field_condition(
            {"field": "episode", "operator": "mod", "value": 2}, _res()
        ) is False


# ---------------------------------------------------------------------------
# String field edges
# ---------------------------------------------------------------------------


class TestStringFieldEdges:
    def test_eq_ne_with_non_string_expected(self):
        r = _res(resolution="1080p")
        assert evaluate_field_condition(
            {"field": "resolution", "operator": "eq", "value": 1080}, r
        ) is False
        assert evaluate_field_condition(
            {"field": "resolution", "operator": "ne", "value": 1080}, r
        ) is True
        assert evaluate_field_condition(
            {"field": "resolution", "operator": "ne", "value": "1080p"}, r
        ) is False

    def test_bad_regex_returns_false(self):
        assert evaluate_field_condition(
            {"field": "title_en", "operator": "regex", "value": "(unclosed"}, _res()
        ) is False

    def test_unknown_string_op_fails_closed(self):
        assert evaluate_field_condition(
            {"field": "title_en", "operator": "startswith", "value": "T"}, _res()
        ) is False


# ---------------------------------------------------------------------------
# get_field_value derivation edges
# ---------------------------------------------------------------------------


class TestGetFieldValueEdges:
    def test_content_type_audio_from_fk(self):
        assert get_field_value(_res(audio_work_id="a-1"), "content_type") == "audio"
        assert evaluate_field_condition(
            {"field": "content_type", "operator": "eq", "value": "audio"},
            _res(audio_work_id="a-1"),
        ) is True

    def test_resource_collection_display_name(self):
        r = _res(collection=SimpleNamespace(title_cn="系列", title_en="Series"))
        assert get_field_value(r, "collection") == "系列"
        r_en = _res(collection=SimpleNamespace(title_cn=None, title_en="Series"))
        assert get_field_value(r_en, "collection") == "Series"
        assert get_field_value(_res(), "collection") is None

    def test_work_collection_display_name(self):
        series = SimpleNamespace(
            collection=SimpleNamespace(title_cn=None, title_en="Ghost in the Shell")
        )
        assert get_field_value(_res(series=series), "series.collection") == (
            "Ghost in the Shell"
        )
        # Work loaded but its collection relation not loaded → None.
        bare = _res(series=SimpleNamespace(title_cn="剧"))
        assert get_field_value(bare, "series.collection") is None
