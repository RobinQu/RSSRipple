"""Trivial instantiation tests for Pydantic schemas to ensure they're exercised.

Covers schemas that would otherwise be 0% covered just because the tests never
imported them directly.
"""

from __future__ import annotations

import pytest

from app.schemas.dashboard import ActiveDownloadGroup, ActiveDownloadTask, DashboardData
from app.schemas.filter_dsl import BoolCondition, FieldCondition


def test_field_condition_instantiates():
    fc = FieldCondition(field="resolution", operator="eq", value="1080p")
    assert fc.field == "resolution"
    assert fc.operator == "eq"
    assert fc.value == "1080p"


def test_bool_condition_instantiates():
    bc = BoolCondition(
        combinator="and",
        conditions=[FieldCondition(field="resolution", operator="eq", value="1080p")],
        is_not=False,
    )
    assert bc.combinator == "and"
    assert len(bc.conditions) == 1


def test_bool_condition_accepts_raw_dict():
    bc = BoolCondition.model_validate({
        "combinator": "or",
        "conditions": [{"field": "container", "operator": "eq", "value": "mkv"}],
    })
    assert bc.combinator == "or"


def test_dashboard_schemas_instantiate():
    task = ActiveDownloadTask(
        task_id="t", resource_title="R", progress=0.5,
        agent_id="a", agent_name="A", channel_id="c", channel_name="C",
    )
    grp = ActiveDownloadGroup(type="series", id="s", title="S", poster_url=None, tasks=[task])
    data = DashboardData(
        active_agents=1, active_channels=1, active_download_count=1,
        active_download_groups=[grp], pending_decisions=[],
        pending_decisions_total=3, pending_confirmations_total=2,
        pending_plans_total=1,
    )
    assert data.active_agents == 1
    assert len(data.active_download_groups) == 1
    assert data.pending_decisions_total == 3
    assert data.pending_confirmations_total == 2
    assert data.pending_plans_total == 1


# ---------------------------------------------------------------------------
# Channel schema validators
# ---------------------------------------------------------------------------


def test_channel_create_default_required_fields():
    from app.schemas.channel import ChannelCreate

    c = ChannelCreate(name="x", url="https://x", field_mapping={})
    assert "search_title" in c.required_metadata_fields


def test_channel_create_validators_normalize():
    from app.schemas.channel import ChannelCreate

    c = ChannelCreate(
        name="x",
        url="https://x",
        field_mapping={},
        metadata_source="Wikipedia",
        metadata_fallback_sources=["Bangumi", "bangumi", "tmdb"],
        required_metadata_fields=["episode"],
        auto_cleanup_unresolved_days=0,
        metadata_refresh_interval_minutes=10,
    )
    assert c.metadata_source == "wikipedia"
    assert c.metadata_fallback_sources == ["bangumi", "tmdb"]
    assert "search_title" in c.required_metadata_fields
    assert c.auto_cleanup_unresolved_days == 1
    assert c.metadata_refresh_interval_minutes == 30


def test_channel_update_validators_normalize():
    from app.schemas.channel import ChannelUpdate

    u = ChannelUpdate(
        metadata_source="TMDB",
        metadata_fallback_sources=["Mal", "mal"],
        required_metadata_fields=["episode"],
        auto_cleanup_unresolved_days=400,
        metadata_refresh_interval_minutes=None,
    )
    assert u.metadata_source == "tmdb"
    assert u.metadata_fallback_sources == ["mal"]
    assert u.auto_cleanup_unresolved_days == 365
    assert u.metadata_refresh_interval_minutes is None


def test_channel_normalizer_helpers():
    from app.schemas.channel import (
        _clamp_cleanup_days,
        _clamp_refresh_interval_minutes,
        _normalize_fallback_sources,
        _normalize_required_fields,
        _normalize_source,
    )

    assert _clamp_cleanup_days(30) == 30
    assert _clamp_cleanup_days(0) == 1
    assert _clamp_cleanup_days(999) == 365
    assert _clamp_refresh_interval_minutes(None) is None
    assert _clamp_refresh_interval_minutes(10) == 30
    assert _clamp_refresh_interval_minutes(20000) == 10080
    assert _normalize_source(None) is None
    assert _normalize_source("  ") is None
    assert _normalize_source("Wikipedia") == "wikipedia"
    assert _normalize_fallback_sources(None) is None
    assert _normalize_fallback_sources(["Mal", "mal"]) == ["mal"]
    with pytest.raises(ValueError):
        _normalize_fallback_sources(["nope"])
    with pytest.raises(ValueError):
        _normalize_source("exa")
    with pytest.raises(ValueError):
        _normalize_required_fields(None)
    with pytest.raises(ValueError):
        _normalize_required_fields(["not-a-field"])


def test_summarize_filters_request_decodes_bytes():
    from app.schemas.channel import SummarizeFiltersRequest

    req = SummarizeFiltersRequest.model_validate(b'{"resource_ids": ["a", "b"]}')
    assert req.resource_ids == ["a", "b"]
    req2 = SummarizeFiltersRequest.model_validate({"resource_ids": []})
    assert req2.resource_ids == []
