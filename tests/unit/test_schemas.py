"""Trivial instantiation tests for Pydantic schemas to ensure they're exercised.

Covers schemas that would otherwise be 0% covered just because the tests never
imported them directly.
"""

from __future__ import annotations

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
    )
    assert data.active_agents == 1
    assert len(data.active_download_groups) == 1
