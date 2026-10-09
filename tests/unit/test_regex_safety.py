"""Tests for the ReDoS guard on user-supplied filter regexes."""

from __future__ import annotations

import logging
from types import SimpleNamespace

from app.services.filter_engine import (
    MAX_REGEX_LENGTH,
    check_regex_safety,
    evaluate_field_condition,
    validate_filter_config,
)


def _res(**overrides):
    defaults = dict(title_en="Title", subtitle_group="LoliHouse")
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _validate_regex(pattern):
    return validate_filter_config(
        {"field": "title_en", "operator": "regex", "value": pattern}
    )


class TestCheckRegexSafety:
    def test_safe_patterns_pass(self):
        for pattern in (
            r"^Title.*",
            r"\d{2,4}",
            r"(1080p|720p)",
            r"(?:CHS|CHT)",
            r"^\[.*?\]",
            r"[+*]",          # quantifier chars inside a class are literals
            r"\(\+\)",        # escaped parens / quantifiers
            r"a+",
            r"(?P<grp>ab+)c", # single-level quantifier inside a group, group not quantified
        ):
            assert check_regex_safety(pattern) is None, pattern

    def test_too_long_rejected(self):
        pattern = "a" * (MAX_REGEX_LENGTH + 1)
        err = check_regex_safety(pattern)
        assert err is not None and "too long" in err

    def test_length_at_cap_accepted(self):
        assert check_regex_safety("a" * MAX_REGEX_LENGTH) is None

    def test_nested_quantifiers_rejected(self):
        for pattern in (
            r"(a+)+",
            r"(a*)*",
            r"(a?)+",
            r"([0-9]+)*$",
            r"(x|y+)+",
            r"((ab)+)+",
            r"(ab+){2,}",
            r"(a|b+)*c",
        ):
            err = check_regex_safety(pattern)
            assert err is not None and "unsafe" in err, pattern


class TestValidationIntegration:
    def test_save_time_validation_rejects_unsafe(self):
        errs = _validate_regex(r"(a+)+")
        assert any("unsafe regex" in e for e in errs)

    def test_save_time_validation_rejects_too_long(self):
        errs = _validate_regex("a" * (MAX_REGEX_LENGTH + 1))
        assert any("too long" in e for e in errs)

    def test_save_time_validation_accepts_safe(self):
        assert _validate_regex(r"^Title \d{4}") == []

    def test_invalid_regex_still_reported(self):
        errs = _validate_regex("(unclosed")
        assert any("invalid regex" in e for e in errs)


class TestEvaluationBackstop:
    """Pre-existing (pre-guard) configs may hold unsafe patterns: evaluation
    must skip them as no-match with a warning, never hang."""

    def test_unsafe_pattern_evaluates_false_with_warning(self, caplog):
        cond = {"field": "title_en", "operator": "regex", "value": r"(T+)+$"}
        with caplog.at_level(logging.WARNING, logger="app.services.filter_engine"):
            assert evaluate_field_condition(cond, _res()) is False
        assert any("unsafe" in r.message for r in caplog.records)

    def test_unsafe_pattern_on_subtitle_groups(self, caplog):
        cond = {"field": "subtitle_groups", "operator": "regex", "value": r"(L+)+$"}
        with caplog.at_level(logging.WARNING, logger="app.services.filter_engine"):
            assert evaluate_field_condition(cond, _res()) is False
        assert any("unsafe" in r.message for r in caplog.records)

    def test_safe_pattern_still_matches(self, caplog):
        cond = {"field": "title_en", "operator": "regex", "value": r"^Titl"}
        with caplog.at_level(logging.WARNING, logger="app.services.filter_engine"):
            assert evaluate_field_condition(cond, _res()) is True
        assert not caplog.records

    def test_invalid_pattern_evaluates_false(self):
        cond = {"field": "title_en", "operator": "regex", "value": "(unclosed"}
        assert evaluate_field_condition(cond, _res()) is False
