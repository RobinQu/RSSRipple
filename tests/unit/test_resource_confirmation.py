from datetime import date
from types import SimpleNamespace

from app.services.resource_confirmation import inspect_resource_confirmation


def _resource(**overrides):
    values = {
        "series_id": "series-1",
        "movie_id": None,
        "audio_work_id": None,
        "collection_id": None,
        "series": SimpleNamespace(
            start_date=None,
            is_anime=False,
            rating=None,
            genre=None,
            collection=None,
        ),
        "is_batch": False,
        "batch_scope": None,
        "batch_seasons": None,
        "season": 1,
        "episode": 2,
        "episode_confidence": "raw",
        "title_cn": "中文名",
        "title_en": "English",
        "search_title": "title",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_unlinked_metadata_is_channel_confirmation():
    result = inspect_resource_confirmation(
        _resource(series_id=None, series=None), required_metadata_fields=[]
    )
    assert result.kinds == ("metadata_unlinked",)


def test_ambiguous_episode_and_missing_required_fields_are_combined():
    result = inspect_resource_confirmation(
        _resource(episode_confidence="ambiguous"),
        required_metadata_fields=["year"],
    )
    assert result.kinds == (
        "episode_ambiguous",
        "required_fields_missing",
    )
    assert "year" in result.missing_fields


def test_batch_unknown_coverage_is_resource_confirmation():
    result = inspect_resource_confirmation(
        _resource(is_batch=True, batch_scope="multi_season", season=None),
        required_metadata_fields=None,
    )
    assert result.kinds == ("batch_coverage_unknown",)


def test_multi_season_coverage_does_not_require_flat_season_range():
    result = inspect_resource_confirmation(
        _resource(
            is_batch=True,
            batch_scope="multi_season",
            season=None,
                batch_seasons=[0, 1],
                series=SimpleNamespace(
                    start_date=date(2020, 1, 1), is_anime=False,
                    rating=None, genre=None, collection=None,
                ),
        ),
        required_metadata_fields=["season", "episode_start", "episode_end"],
    )
    assert result.kinds == ()
    assert result.missing_fields == ()


def test_audio_work_never_gets_batch_coverage_confirmation():
    result = inspect_resource_confirmation(
        _resource(
            series_id=None,
            series=None,
            audio_work_id="audio-1",
            is_batch=True,
            batch_scope="season",
            season=None,
        ),
        required_metadata_fields=["season", "episode_start", "episode_end"],
    )
    assert result.kinds == ()


def test_franchise_collection_has_no_single_content_type_requirement():
    result = inspect_resource_confirmation(
        _resource(
            series_id=None,
            series=None,
            collection_id="collection-1",
            collection=SimpleNamespace(title_cn="福星小子", title_en=None),
            is_batch=True,
            batch_scope="franchise",
        ),
        required_metadata_fields=["content_type", "resource_collection"],
    )
    assert result.kinds == ()
