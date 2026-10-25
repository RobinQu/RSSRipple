from datetime import date
from types import SimpleNamespace

import pytest

from app.services.channel_metadata_defaults import (
    clear_channel_default,
    normalize_required_metadata_defaults,
    reconcile_channel_metadata_defaults,
)
from app.services.resource_confirmation import inspect_resource_confirmation


def _resource(**overrides):
    values = {
        "id": "r1",
        "series_id": None,
        "movie_id": "m1",
        "audio_work_id": None,
        "collection_id": None,
        "work_links": [],
        "movie": SimpleNamespace(release_date=date(2013, 1, 1), is_anime=False),
        "search_title": "2 Guns",
        "is_batch": False,
        "episode_confidence": "manual",
        "resolution": None,
        "subtitle_group": None,
        "subtitle_groups": None,
        "subtitle_groups_source": None,
        "subtitle_langs": None,
        "channel_default_fields": [],
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_normalize_defaults_validates_keys_membership_and_values():
    assert normalize_required_metadata_defaults(
        {"resolution": " 2160p ", "subtitle_langs": ["zh-CN", "en", "zh-CN"]},
        ["resolution", "subtitle_langs"],
    ) == {"resolution": "2160p", "subtitle_langs": ["zh-CN", "en"]}
    with pytest.raises(ValueError, match="does not support"):
        normalize_required_metadata_defaults({"episode": 1})
    with pytest.raises(ValueError, match="non-required"):
        normalize_required_metadata_defaults({"resolution": "2160p"}, [])
    with pytest.raises(ValueError, match="non-empty"):
        normalize_required_metadata_defaults({"resolution": " "})
    with pytest.raises(ValueError, match="string array"):
        normalize_required_metadata_defaults({"subtitle_langs": "zh-CN"})


def test_resolution_default_passes_required_gate_and_marks_provenance():
    resource = _resource()
    assert inspect_resource_confirmation(resource, ["resolution"]).required

    changed = reconcile_channel_metadata_defaults(resource, {"resolution": "2160p"})

    assert changed
    assert resource.resolution == "2160p"
    assert resource.channel_default_fields == ["resolution"]
    confirmation = inspect_resource_confirmation(resource, ["resolution"])
    assert not confirmation.required
    assert confirmation.missing_fields == ()


def test_default_does_not_bypass_unlinked_gate():
    resource = _resource(movie_id=None, movie=None)
    reconcile_channel_metadata_defaults(resource, {"resolution": "2160p"})
    assert inspect_resource_confirmation(resource, ["resolution"]).kinds == (
        "metadata_unlinked",
    )


def test_reconcile_preserves_real_values_and_updates_or_removes_defaults():
    resource = _resource(source="BluRay")
    reconcile_channel_metadata_defaults(
        resource, {"resolution": "2160p", "source": "WEB-DL"}
    )
    assert resource.resolution == "2160p"
    assert resource.source == "BluRay"
    assert resource.channel_default_fields == ["resolution"]

    resource.resolution = "1080p"
    clear_channel_default(resource, "resolution")
    reconcile_channel_metadata_defaults(resource, {"resolution": "4K"})
    assert resource.resolution == "1080p"
    assert resource.channel_default_fields == []

    resource.resolution = None
    reconcile_channel_metadata_defaults(resource, {"resolution": "2160p"})
    reconcile_channel_metadata_defaults(resource, {"resolution": "4K"})
    assert resource.resolution == "4K"
    reconcile_channel_metadata_defaults(resource, {})
    assert resource.resolution is None
    assert resource.channel_default_fields == []


def test_subtitle_group_and_languages_defaults_synchronize_fields():
    resource = _resource()
    reconcile_channel_metadata_defaults(
        resource,
        {"subtitle_group": "Group", "subtitle_langs": ["zh-CN", "en"]},
    )
    assert resource.subtitle_group == "Group"
    assert resource.subtitle_groups == ["Group"]
    assert resource.subtitle_groups_source == "channel_default"
    assert resource.subtitle_langs == ["zh-CN", "en"]

    clear_channel_default(resource, "subtitle_group")
    resource.subtitle_group = "Manual"
    resource.subtitle_groups = ["Manual"]
    resource.subtitle_groups_source = "manual"
    reconcile_channel_metadata_defaults(resource, {"subtitle_group": "Default"})
    assert resource.subtitle_group == "Manual"
    assert resource.subtitle_groups_source == "manual"


def test_real_metadata_replaces_subtitle_group_default():
    from app.services.metadata_repository import _fill_subtitle_group

    resource = _resource()
    reconcile_channel_metadata_defaults(resource, {"subtitle_group": "Default"})
    _fill_subtitle_group(
        SimpleNamespace(subtitle_groups=["Real"], subtitle_group=None), resource
    )
    assert resource.subtitle_group == "Real"
    assert resource.subtitle_groups == ["Real"]
    assert resource.subtitle_groups_source == "llm"
    assert resource.channel_default_fields == []
