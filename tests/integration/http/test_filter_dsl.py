"""Filter DSL operator matrix integration tests.

Exercises evaluate_filter_config / merge_filters / validate_filter_config
through POST /agents/{id}/test-filters on real fetched resources:

  - string ops: eq / ne / contains / fuzzy / in / regex (case-insensitive)
  - number ops: eq / gt / gte / lt / lte / in on episode
  - list ops: contains on subtitle_langs
  - empty semantics: is_empty / is_not_empty
  - nested BoolCondition groups and is_not negation
  - validation: unknown field / bad operator / empty value / bad regex → 422

Feed layout (mikanani series=3, 咒术回战): 6 episodes × 3 groups:
  LoliHouse 1080p, ANi 1080p, Skymoon-Raws 720p → 18 resources.

Requirements: Docker test environment with app + test-server services.
"""

from __future__ import annotations

import pytest

from tests.integration.http._http import (
    RICH_FIELD_MAPPING,
    TEST_SERVER,
    _api,
    _poll_fetch,
)

FEED_URL = f"{TEST_SERVER}/rss/mikanani?series=3"


def _cond(field: str, operator: str, value=None) -> dict:
    c = {"field": field, "operator": operator}
    if value is not None:
        c["value"] = value
    return c


def _and(*conditions) -> dict:
    return {"combinator": "and", "conditions": list(conditions)}


def _or(*conditions) -> dict:
    return {"combinator": "or", "conditions": list(conditions)}


@pytest.fixture(scope="class")
def _dsl_agent():
    """Channel with 18 parsed resources + an agent with no filter."""
    r = _api(
        "/api/v1/channels",
        method="post",
        json={
            "name": "DSL Matrix Channel",
            "url": FEED_URL,
            "field_mapping": RICH_FIELD_MAPPING,
            "fetch_interval": 3600,
            "metadata_agent_enabled": False,
        },
    )
    if r.status_code != 201:
        pytest.skip(f"Channel creation failed: {r.status_code} {r.text}")
    ch_id = r.json()["data"]["id"]

    _api(f"/api/v1/channels/{ch_id}/fetch", method="post")
    result = _poll_fetch(ch_id, accept_failed=True)
    if result.get("status") != "done":
        _api(f"/api/v1/channels/{ch_id}", method="delete")
        pytest.skip(f"Fetch did not complete: {result}")

    r = _api("/api/v1/downloaders", params={"page_size": 100})
    dl_id = None
    for d in r.json().get("data", []):
        if d.get("type") == "mock":
            dl_id = d["id"]
            break
    if not dl_id:
        r = _api(
            "/api/v1/downloaders",
            method="post",
            json={"name": "DSL Mock Downloader", "type": "mock"},
        )
        if r.status_code != 201:
            _api(f"/api/v1/channels/{ch_id}", method="delete")
            pytest.skip(f"downloader setup failed: {r.text}")
        dl_id = r.json()["data"]["id"]

    r = _api(
        "/api/v1/agents",
        method="post",
        json={
            "name": "DSL Matrix Agent",
            "channel_id": ch_id,
            "downloader_id": dl_id,
            "scope_channel_wide": True,
            "llm_enabled": False,
            "conflict_resolution": "auto",
        },
    )
    if r.status_code != 201:
        _api(f"/api/v1/channels/{ch_id}", method="delete")
        pytest.skip(f"Agent creation failed: {r.status_code} {r.text}")
    agent_id = r.json()["data"]["id"]

    yield agent_id

    try:
        _api(f"/api/v1/agents/{agent_id}", method="delete")
        _api(f"/api/v1/channels/{ch_id}", method="delete")
    except Exception:
        pass


class TestFilterDSLOperators:
    """Each case: set filter_config, then test-filters and count passes."""

    def _passed(self, agent_id: str, filter_config: dict) -> int:
        r = _api(
            f"/api/v1/agents/{agent_id}",
            method="put",
            json={"filter_config": filter_config},
        )
        assert r.status_code == 200, f"filter update failed: {r.text}"
        r = _api(f"/api/v1/agents/{agent_id}/test-filters", method="post", json={})
        assert r.status_code == 200, f"test-filters failed: {r.text}"
        data = r.json()["data"]
        assert data["total"] == 18, f"expected 18 resources, got {data['total']}"
        return data["passed"]

    def test_string_eq_case_insensitive(self, _dsl_agent):
        assert self._passed(_dsl_agent, _and(_cond("resolution", "eq", "1080P"))) == 12

    def test_string_ne(self, _dsl_agent):
        assert self._passed(_dsl_agent, _and(_cond("resolution", "ne", "1080p"))) == 6

    def test_string_contains(self, _dsl_agent):
        # "ani" matches "ANi" case-insensitively
        assert self._passed(_dsl_agent, _and(_cond("subtitle_group", "contains", "ani"))) == 6

    def test_string_fuzzy(self, _dsl_agent):
        assert self._passed(_dsl_agent, _and(_cond("subtitle_group", "fuzzy", "LoliHous"))) == 6

    def test_string_in(self, _dsl_agent):
        assert self._passed(
            _dsl_agent, _and(_cond("resolution", "in", ["720p", "2160p"]))
        ) == 6

    def test_string_regex(self, _dsl_agent):
        assert self._passed(_dsl_agent, _and(_cond("subtitle_group", "regex", "^Skymoon"))) == 6

    def test_number_eq(self, _dsl_agent):
        assert self._passed(_dsl_agent, _and(_cond("episode", "eq", 1))) == 3

    def test_number_gt_gte(self, _dsl_agent):
        assert self._passed(_dsl_agent, _and(_cond("episode", "gt", 3))) == 9
        assert self._passed(_dsl_agent, _and(_cond("episode", "gte", 3))) == 12

    def test_number_lt_lte(self, _dsl_agent):
        assert self._passed(_dsl_agent, _and(_cond("episode", "lt", 3))) == 6
        assert self._passed(_dsl_agent, _and(_cond("episode", "lte", 3))) == 9

    def test_number_in(self, _dsl_agent):
        assert self._passed(_dsl_agent, _and(_cond("episode", "in", [1, 2]))) == 6

    def test_is_empty_and_is_not_empty(self, _dsl_agent):
        # file_size is never parsed by the mapping → empty everywhere
        assert self._passed(_dsl_agent, _and(_cond("file_size", "is_empty"))) == 18
        assert self._passed(_dsl_agent, _and(_cond("file_size", "is_not_empty"))) == 0
        assert self._passed(_dsl_agent, _and(_cond("subtitle_group", "is_not_empty"))) == 18

    def test_list_contains(self, _dsl_agent):
        # 简繁内封 → [zh-CN, zh-TW]; 简体 → [zh-CN]; CHT → [zh-TW]
        assert self._passed(_dsl_agent, _and(_cond("subtitle_langs", "contains", "zh-CN"))) == 12
        assert self._passed(_dsl_agent, _and(_cond("subtitle_langs", "contains", "zh-TW"))) == 12

    def test_nested_groups(self, _dsl_agent):
        f = _and(
            _or(_cond("resolution", "eq", "1080p"), _cond("resolution", "eq", "720p")),
            _cond("episode", "gte", 5),
        )
        assert self._passed(_dsl_agent, f) == 6

    def test_is_not_group(self, _dsl_agent):
        f = _and(_cond("resolution", "eq", "1080p"))
        f["is_not"] = True
        assert self._passed(_dsl_agent, f) == 6

    def test_null_value_ne_passes_but_eq_fails(self, _dsl_agent):
        # container is never parsed on these resources (NULL): value-taking
        # ops (eq) fail on empty, ne passes — per the DSL empty-value rules.
        assert self._passed(_dsl_agent, _and(_cond("container", "ne", "mkv"))) == 18
        assert self._passed(_dsl_agent, _and(_cond("container", "eq", "mkv"))) == 0


class TestFilterDSLValidation:
    """Invalid filters are rejected at save time with 422."""

    def _put(self, agent_id: str, filter_config: dict) -> int:
        r = _api(
            f"/api/v1/agents/{agent_id}",
            method="put",
            json={"filter_config": filter_config},
        )
        return r.status_code

    def test_unknown_operator_422(self, _dsl_agent):
        assert self._put(_dsl_agent, _and(_cond("resolution", "bogus", "x"))) == 422

    def test_empty_value_422(self, _dsl_agent):
        assert self._put(_dsl_agent, _and(_cond("resolution", "eq", ""))) == 422

    def test_wrong_op_for_field_type_422(self, _dsl_agent):
        # episode is numeric → string op rejected
        assert self._put(_dsl_agent, _and(_cond("episode", "contains", "1"))) == 422

    def test_in_comma_string_accepted(self, _dsl_agent):
        # 'in' accepts a comma-separated string (coerced to a list)
        r = _api(
            f"/api/v1/agents/{_dsl_agent}",
            method="put",
            json={"filter_config": _and(_cond("resolution", "in", "1080p,720p"))},
        )
        assert r.status_code == 200
        r = _api(f"/api/v1/agents/{_dsl_agent}/test-filters", method="post", json={})
        assert r.json()["data"]["passed"] == 18

    def test_in_empty_list_422(self, _dsl_agent):
        assert self._put(_dsl_agent, _and(_cond("resolution", "in", []))) == 422

    def test_bad_regex_422(self, _dsl_agent):
        assert self._put(_dsl_agent, _and(_cond("resolution", "regex", "["))) == 422

    def test_unknown_field_422(self, _dsl_agent):
        assert self._put(_dsl_agent, _and(_cond("nope", "eq", "x"))) == 422
