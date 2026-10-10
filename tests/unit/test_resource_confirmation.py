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


def test_legacy_season_list_does_not_prove_episode_coverage():
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
    assert result.kinds == ("batch_coverage_unknown",)
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


# ---------------------------------------------------------------------------
# Links-carried multi-season packs (per-season works)
# ---------------------------------------------------------------------------


def _links_only_pack(*, links, batch_seasons):
    """Terminal multi_season pack: flat work FKs cleared, works on links."""
    return _resource(
        series_id=None,
        series=None,
        is_batch=True,
        batch_scope="multi_season",
        season=None,
        batch_seasons=batch_seasons,
        work_links=links,
    )


def test_links_only_multi_season_pack_is_linked_and_covered():
    links = [
        SimpleNamespace(series_id="s-1", movie_id=None, series=SimpleNamespace(id="s-1", season_number=1)),
        SimpleNamespace(series_id="s-2", movie_id=None, series=SimpleNamespace(id="s-2", season_number=2)),
    ]
    pack = _links_only_pack(links=links, batch_seasons=[1, 2])
    pack.file_assignments = [
        SimpleNamespace(series_id=link.series_id, movie_id=None, season=link.series.season_number,
                        episode_start=1, episode_end=12)
        for link in links
    ]
    result = inspect_resource_confirmation(pack, required_metadata_fields=None)
    assert result.kinds == ()


def test_links_only_multi_season_pack_without_links_is_unknown_coverage():
    result = inspect_resource_confirmation(
        _links_only_pack(links=[], batch_seasons=[1, 2]),
        required_metadata_fields=None,
    )
    assert "metadata_unlinked" in result.kinds
    assert "batch_coverage_unknown" in result.kinds


def test_links_only_multi_season_pack_requires_derived_batch_seasons():
    links = [
        SimpleNamespace(series_id="s-1", movie_id=None, series=SimpleNamespace(season_number=1)),
    ]
    # batch_seasons 缺失 → 覆盖度未知。
    result = inspect_resource_confirmation(
        _links_only_pack(links=links, batch_seasons=None),
        required_metadata_fields=None,
    )
    assert result.kinds == ("batch_coverage_unknown",)
    # batch_seasons 与关联作品 season_number 派生不一致 → 覆盖度未知。
    result = inspect_resource_confirmation(
        _links_only_pack(links=links, batch_seasons=[2]),
        required_metadata_fields=None,
    )
    assert result.kinds == ("batch_coverage_unknown",)


def test_links_only_pack_unloaded_link_table_is_not_judged():
    """Link table not loaded (list endpoints): coverage can't be judged here."""
    result = inspect_resource_confirmation(
        _resource(
            series_id=None, series=None, collection_id=None,
            is_batch=True, batch_scope="multi_season",
            season=None, batch_seasons=[1, 2],
        ),
        required_metadata_fields=None,
    )
    # No work_links attribute → loaded_relation yields None → no
    # links-based verdict; still unlinked at the flat-FK level.
    assert result.kinds == ("metadata_unlinked",)


# ---------------------------------------------------------------------------
# Dispatch-grade coverage gate: scope restriction and extras tolerance
# ---------------------------------------------------------------------------


def test_franchise_pack_is_exempt_from_dispatch_coverage_gate():
    """franchise packs never dispatch; coverage validation would be backlog."""
    links = [
        SimpleNamespace(series_id="s-1", movie_id=None,
                        series=SimpleNamespace(id="s-1", season_number=1)),
    ]
    result = inspect_resource_confirmation(
        _resource(
            series_id=None,
            series=None,
            is_batch=True,
            batch_scope="franchise",
            season=None,
            work_links=links,
            file_assignments=[
                SimpleNamespace(series_id=None, movie_id=None, season=None,
                                episode_start=None, episode_end=None),
            ],
        ),
        required_metadata_fields=None,
    )
    assert result.kinds == ()


def test_season_scope_is_still_gated_on_authoritative_coverage():
    links = [
        SimpleNamespace(series_id="s-1", movie_id=None,
                        series=SimpleNamespace(id="s-1", season_number=1)),
    ]
    result = inspect_resource_confirmation(
        _resource(
            series_id=None,
            series=None,
            is_batch=True,
            batch_scope="season",
            season=None,
            work_links=links,
            file_assignments=[
                SimpleNamespace(series_id="s-1", movie_id=None, season=2,
                                episode_start=1, episode_end=12),
            ],
        ),
        required_metadata_fields=None,
    )
    assert result.kinds == ("batch_coverage_unknown",)


def test_season_pack_with_specials_extras_is_covered():
    result = inspect_resource_confirmation(
        _resource(
            is_batch=True,
            batch_scope="season",
            season=1,
            series=SimpleNamespace(
                id="series-1", season_number=1, start_date=None,
                is_anime=False, rating=None, genre=None, collection=None,
            ),
            work_links=[],
            file_assignments=[
                SimpleNamespace(series_id="series-1", movie_id=None, season=1,
                                episode_start=1, episode_end=12),
                SimpleNamespace(series_id="series-1", movie_id=None, season=0,
                                episode_start=1, episode_end=3),
            ],
        ),
        required_metadata_fields=None,
    )
    assert result.kinds == ()
