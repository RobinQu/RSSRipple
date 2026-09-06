"""In-process coverage for small DB/pure services.

Targets branches the HTTP suites do not reach:

- ``app.services.required_fields`` — per-shape locked keys, row-shape
  derivation edges (audio / links-carried packs), the work-link fallback in
  semantic value resolution, blank/empty "missing" semantics, and the agent
  filter gating helpers.
- ``app.services.resource_confirmation`` — the batch-coverage-unknown matrix
  (flat-FK packs and links-carried multi-season packs).
- ``app.services.settings_service`` — bool/int coercion, defaults, clamping.
- ``app.services.volume_service`` — volume-bound path resolution errors and
  library root/recycle derivation.
- ``app.services.text_normalizer`` — helper edges (empty/single-char inputs)
  and the OpenCC-unavailable fallback.
"""

from __future__ import annotations

import sys
from datetime import date
from types import SimpleNamespace

import pytest

from app.services import required_fields as rf
from app.services import text_normalizer as tn
from app.services.resource_confirmation import inspect_resource_confirmation
from app.services.settings_service import (
    get_bool_setting,
    get_int_setting,
    get_setting,
    set_setting,
)
from app.services.volume_service import (
    VolumeResolutionError,
    check_mount,
    resolve_downloader_path,
    resolve_library_recycle,
    resolve_library_root,
)


def _series_link(season_number: int | None, start: date | None = None, **extra):
    work = SimpleNamespace(
        season_number=season_number,
        start_date=start,
        release_date=None,
        rating=8.5,
        is_anime=True,
    )
    return SimpleNamespace(
        series_id=f"s-{season_number}", series=work, movie_id=None, movie=None, **extra
    )


# ── required_fields ─────────────────────────────────────────────────────────


class TestRequiredKeysForShape:
    def test_tv_single_gets_base_and_episode(self):
        keys = rf.required_keys_for_shape(rf.TV_SINGLE)
        assert {"search_title", "content_type", "is_batch", "year", "is_anime", "episode"} <= keys
        assert "episode_start" not in keys

    def test_franchise_excludes_work_scoped_base_keys(self):
        keys = rf.required_keys_for_shape(rf.FRANCHISE)
        # year/is_anime/content_type need a linked work; franchise packs have none.
        assert "year" not in keys
        assert "is_anime" not in keys
        assert "resource_collection" in keys
        assert {"search_title", "is_batch"} <= keys

    def test_unknown_shape_keeps_only_unrestricted_base(self):
        keys = rf.required_keys_for_shape("something_else")
        assert keys == frozenset({"search_title", "is_batch"})


class TestResourceShape:
    def test_audio_resource(self):
        r = SimpleNamespace(audio_work_id="a1")
        assert rf.resource_shape(r) == rf.AUDIO

    def test_series_batch_multi_season_scope(self):
        r = SimpleNamespace(
            audio_work_id=None, collection_id=None, movie_id=None,
            series_id="s1", is_batch=True, batch_scope="multi_season",
        )
        assert rf.resource_shape(r) == rf.TV_MULTI_SEASON

    def test_series_batch_unknown_scope_has_no_shape(self):
        r = SimpleNamespace(
            audio_work_id=None, collection_id=None, movie_id=None,
            series_id="s1", is_batch=True, batch_scope=None,
        )
        assert rf.resource_shape(r) is None

    def test_links_carried_multi_season_pack(self):
        r = SimpleNamespace(
            audio_work_id=None, collection_id=None, movie_id=None,
            series_id=None, is_batch=True, batch_scope="multi_season",
        )
        assert rf.resource_shape(r) == rf.TV_MULTI_SEASON

    def test_franchise_via_collection_or_scope(self):
        base = dict(
            audio_work_id=None, movie_id=None, series_id=None,
            is_batch=True, batch_scope=None,
        )
        assert rf.resource_shape(SimpleNamespace(collection_id="c1", **base)) == rf.FRANCHISE
        assert (
            rf.resource_shape(
                SimpleNamespace(collection_id=None, **{**base, "batch_scope": "franchise"})
            )
            == rf.FRANCHISE
        )

    def test_movie_and_season_batch_shapes(self):
        base = dict(audio_work_id=None, collection_id=None, series_id=None)
        assert (
            rf.resource_shape(
                SimpleNamespace(movie_id="m1", is_batch=False, batch_scope=None, **base)
            )
            == rf.MOVIE
        )
        r = SimpleNamespace(
            audio_work_id=None, collection_id=None, movie_id=None,
            series_id="s1", is_batch=True, batch_scope="season",
        )
        assert rf.resource_shape(r) == rf.TV_SEASON_BATCH

    def test_unlinked_non_batch_has_no_shape(self):
        r = SimpleNamespace(
            audio_work_id=None, collection_id=None, movie_id=None,
            series_id=None, is_batch=False, batch_scope=None,
        )
        assert rf.resource_shape(r) is None
        # …and a shapeless resource reports nothing missing.
        assert rf.missing_required_fields(r, ["episode"]) == []


class TestSemanticFieldValueViaLinks:
    """Links-carried multi-season packs: flat FKs are cleared, values resolve
    through the work_links table."""

    def _pack(self, links):
        # search_title/is_batch satisfy the locked base keys so each test can
        # isolate the field under test.
        return SimpleNamespace(
            audio_work_id=None, collection_id=None, movie_id=None, series_id=None,
            is_batch=True, batch_scope="multi_season", work_links=links,
            search_title="Pack",
        )

    def test_content_type_derived_from_series_link(self):
        pack = self._pack([_series_link(1, date(2020, 4, 1))])
        assert rf.missing_required_fields(pack, ["content_type"]) == []

    def test_content_type_derived_from_movie_link(self):
        link = SimpleNamespace(
            series_id=None, series=None, movie_id="m1",
            movie=SimpleNamespace(
                release_date=date(2019, 1, 1), rating=7.0, is_anime=False
            ),
        )
        pack = self._pack([link])
        assert rf.missing_required_fields(pack, ["content_type"]) == []

    def test_content_type_missing_when_no_link_work(self):
        pack = self._pack([])
        assert "content_type" in rf.missing_required_fields(pack, ["content_type"])

    def test_year_aggregates_earliest_across_all_link_works(self):
        links = [
            _series_link(0, None),  # specials season without a date
            _series_link(2, date(2022, 1, 1)),
            _series_link(1, date(2020, 1, 1)),
        ]
        pack = self._pack(links)
        assert rf.missing_required_fields(pack, ["year"]) == []

    def test_year_missing_when_all_link_works_dateless(self):
        pack = self._pack([_series_link(1, None)])
        assert "year" in rf.missing_required_fields(pack, ["year"])

    def test_work_pair_field_resolves_through_first_link_work(self):
        pack = self._pack([_series_link(1, date(2020, 1, 1))])
        assert rf.missing_required_fields(pack, ["rating"]) == []

    def test_work_pair_field_missing_when_link_work_unloaded(self):
        # series_id present but the series relationship is not loaded.
        link = SimpleNamespace(series_id="s1", movie_id=None, movie=None)
        pack = self._pack([link])
        assert "rating" in rf.missing_required_fields(pack, ["rating"])

    def test_link_without_any_work_id_resolves_no_work(self):
        link = SimpleNamespace(series_id=None, series=None, movie_id=None, movie=None)
        pack = self._pack([link])
        missing = rf.missing_required_fields(pack, ["content_type"])
        assert "content_type" in missing


class TestWorkPairViaFlatFK:
    def test_movie_fk_routes_to_movie_fields(self):
        r = SimpleNamespace(
            audio_work_id=None, collection_id=None, series_id=None,
            movie_id="m1", is_batch=False, batch_scope=None,
            search_title="Film",
            movie=SimpleNamespace(
                release_date=date(2019, 5, 1), rating=7.5, is_anime=False
            ),
        )
        assert rf.missing_required_fields(r, ["rating"]) == []


class TestMissingSemantics:
    def _tv_single(self, **overrides):
        # series relation satisfies the locked year/is_anime base keys.
        defaults = dict(
            audio_work_id=None, collection_id=None, movie_id=None,
            series_id="s1", is_batch=False, batch_scope=None,
            search_title="Show", subtitle_langs=["zh-CN"], episode=1,
            series=SimpleNamespace(
                start_date=date(2020, 1, 1), is_anime=True, rating=8.0
            ),
        )
        defaults.update(overrides)
        return SimpleNamespace(**defaults)

    def test_blank_string_counts_as_missing(self):
        r = self._tv_single(search_title="   ")
        assert rf.missing_required_fields(r, ["search_title"]) == ["search_title"]

    def test_empty_collection_counts_as_missing(self):
        r = self._tv_single(subtitle_langs=[])
        assert rf.missing_required_fields(r, ["subtitle_langs"]) == ["subtitle_langs"]

    def test_zero_is_a_valid_value(self):
        r = self._tv_single(episode=0)
        assert rf.missing_required_fields(r, ["episode"]) == []


class TestFilterGating:
    def test_validate_unknown_key(self):
        errors = rf.validate_required_fields(["episode", "bogus"])
        assert errors == ["unknown required metadata field: 'bogus'"]
        assert rf.validate_required_fields(None) == []

    def test_allowed_fields_none_declaration_is_legacy_fallback(self):
        assert rf.allowed_agent_filter_fields(None) is None

    def test_allowed_fields_union_of_resource_and_declared_work_fields(self):
        allowed = rf.allowed_agent_filter_fields(["year"])
        assert allowed is not None
        assert {"episode", "search_title", "series.year", "movie.year"} <= allowed
        assert "series.rating" not in allowed

    def test_collect_fields_ignores_non_dict_nodes(self):
        cfg = {
            "combinator": "and",
            "conditions": [
                None,
                "junk",
                {"field": "episode", "op": "eq", "value": 1},
            ],
        }
        assert rf.validate_filter_against_allowed(cfg, frozenset({"episode"})) == []
        errors = rf.validate_filter_against_allowed(
            {"field": "series.rating", "op": "gt", "value": 8},
            frozenset({"episode"}),
        )
        assert errors == [
            "field 'series.rating' is not in the channel's required metadata fields"
        ]


# ── resource_confirmation ───────────────────────────────────────────────────


class TestBatchCoverageConfirmation:
    def _batch(self, **overrides):
        defaults = dict(
            series_id="s1", movie_id=None, audio_work_id=None, collection_id=None,
            episode_confidence=None, season=1, is_batch=True,
            batch_scope=None, batch_seasons=None,
        )
        defaults.update(overrides)
        return SimpleNamespace(**defaults)

    def test_series_batch_scope_none(self):
        conf = inspect_resource_confirmation(self._batch(), None)
        assert "batch_coverage_unknown" in conf.kinds

    def test_series_batch_season_scope_without_season(self):
        conf = inspect_resource_confirmation(
            self._batch(batch_scope="season", season=None), None
        )
        assert "batch_coverage_unknown" in conf.kinds

    def test_series_batch_season_scope_with_season_is_fine(self):
        conf = inspect_resource_confirmation(
            self._batch(batch_scope="season", season=2), None
        )
        assert "batch_coverage_unknown" not in conf.kinds

    def test_series_batch_multi_season_without_batch_seasons(self):
        conf = inspect_resource_confirmation(
            self._batch(batch_scope="multi_season", batch_seasons=None), None
        )
        assert "batch_coverage_unknown" in conf.kinds

    def test_links_pack_without_series_links(self):
        r = self._batch(
            series_id=None, batch_scope="multi_season",
            work_links=[SimpleNamespace(series_id=None, series=None, movie_id="m1")],
        )
        conf = inspect_resource_confirmation(r, None)
        assert "batch_coverage_unknown" in conf.kinds

    def test_links_pack_without_declared_seasons(self):
        r = self._batch(
            series_id=None, batch_scope="multi_season", batch_seasons=None,
            work_links=[_series_link(1)],
        )
        conf = inspect_resource_confirmation(r, None)
        assert "batch_coverage_unknown" in conf.kinds

    def test_links_pack_declared_mismatch_with_linked_seasons(self):
        r = self._batch(
            series_id=None, batch_scope="multi_season", batch_seasons=[1, 2],
            work_links=[_series_link(1)],
        )
        conf = inspect_resource_confirmation(r, None)
        assert "batch_coverage_unknown" in conf.kinds

    def test_links_pack_declared_matches_linked_seasons(self):
        r = self._batch(
            series_id=None, batch_scope="multi_season", batch_seasons=[1, 2],
            work_links=[_series_link(1), _series_link(2)],
        )
        conf = inspect_resource_confirmation(r, None)
        assert conf.kinds == ()
        assert conf.required is False


class TestOtherConfirmationKinds:
    def _single(self, **overrides):
        defaults = dict(
            series_id="s1", movie_id=None, audio_work_id=None, collection_id=None,
            episode_confidence=None, season=1, is_batch=False, batch_scope=None,
            batch_seasons=None,
        )
        defaults.update(overrides)
        return SimpleNamespace(**defaults)

    def test_unlinked_resource(self):
        r = self._single(series_id=None)
        conf = inspect_resource_confirmation(r, None)
        assert conf.kinds == ("metadata_unlinked",)

    def test_ambiguous_episode_kinds(self):
        conf = inspect_resource_confirmation(
            self._single(episode_confidence="ambiguous", season=None), None
        )
        assert conf.kinds == ("season_ambiguous",)
        conf = inspect_resource_confirmation(
            self._single(episode_confidence="ambiguous", season=1), None
        )
        assert conf.kinds == ("episode_ambiguous",)

    def test_required_fields_missing_kind(self):
        r = self._single(search_title="   ")
        conf = inspect_resource_confirmation(r, ["search_title"])
        assert "required_fields_missing" in conf.kinds
        assert "search_title" in conf.missing_fields


# ── settings_service ────────────────────────────────────────────────────────


async def test_get_setting_roundtrip_and_delete(db_session):
    assert await get_setting(db_session, "misc.key") is None
    await set_setting(db_session, "misc.key", "v1")
    assert await get_setting(db_session, "misc.key") == "v1"
    await set_setting(db_session, "misc.key", "v2")
    assert await get_setting(db_session, "misc.key") == "v2"
    # Empty value deletes the row (flush so the identity map drops it).
    await set_setting(db_session, "misc.key", None)
    await db_session.flush()
    assert await get_setting(db_session, "misc.key") is None
    # Deleting an unset key is a no-op.
    await set_setting(db_session, "misc.key", "")


async def test_get_bool_setting(db_session):
    assert await get_bool_setting(db_session, "misc.bool") is False
    assert await get_bool_setting(db_session, "misc.bool", default=True) is True
    for truthy in ("1", "true", "YES", " on "):
        await set_setting(db_session, "misc.bool", truthy)
        assert await get_bool_setting(db_session, "misc.bool") is True
    await set_setting(db_session, "misc.bool", "nope")
    assert await get_bool_setting(db_session, "misc.bool", default=True) is False


async def test_get_int_setting(db_session):
    assert await get_int_setting(db_session, "misc.int", 1440) == 1440
    await set_setting(db_session, "misc.int", "60")
    assert await get_int_setting(db_session, "misc.int", 1440) == 60
    # Unparseable values fall back to the default.
    await set_setting(db_session, "misc.int", "abc")
    assert await get_int_setting(db_session, "misc.int", 1440) == 1440
    # Clamping on both bounds.
    await set_setting(db_session, "misc.int", "5")
    assert await get_int_setting(db_session, "misc.int", 1440, minimum=30) == 30
    await set_setting(db_session, "misc.int", "99999")
    assert (
        await get_int_setting(db_session, "misc.int", 1440, maximum=10080) == 10080
    )


# ── volume_service ──────────────────────────────────────────────────────────


class TestResolveDownloaderPath:
    def test_identity_without_binding(self):
        assert resolve_downloader_path(None, "/downloads/a") == "/downloads/a"
        dl = SimpleNamespace(volume_id=None)
        assert resolve_downloader_path(dl, "/downloads/a") == "/downloads/a"

    def test_missing_volume_raises(self):
        dl = SimpleNamespace(volume_id="v1", volume=None, name="dl1")
        with pytest.raises(VolumeResolutionError, match="存储卷不存在"):
            resolve_downloader_path(dl, "/downloads/a")

    def test_incomplete_binding_raises(self):
        volume = SimpleNamespace(mount_path="/mnt/shared")
        dl = SimpleNamespace(
            volume_id="v1", volume=volume, name="dl1", download_dir=""
        )
        with pytest.raises(VolumeResolutionError, match="绑定不完整"):
            resolve_downloader_path(dl, "/downloads/a")
        dl.download_dir = "/downloads"
        with pytest.raises(VolumeResolutionError, match="绑定不完整"):
            resolve_downloader_path(dl, "")

    def test_subpath_and_root_and_descendant(self):
        volume = SimpleNamespace(mount_path="/mnt/shared/")
        dl = SimpleNamespace(
            volume_id="v1", volume=volume, name="dl1",
            download_dir="/downloads/", volume_subpath="/complete/",
        )
        assert resolve_downloader_path(dl, "/downloads") == "/mnt/shared/complete"
        assert (
            resolve_downloader_path(dl, "/downloads/show/file.mkv")
            == "/mnt/shared/complete/show/file.mkv"
        )

    def test_path_outside_root_raises(self):
        volume = SimpleNamespace(mount_path="/mnt/shared")
        dl = SimpleNamespace(
            volume_id="v1", volume=volume, name="dl1",
            download_dir="/downloads", volume_subpath=None,
        )
        with pytest.raises(VolumeResolutionError, match="不在下载根"):
            resolve_downloader_path(dl, "/elsewhere/a")


class TestLibraryPaths:
    def test_root_unbound(self):
        assert resolve_library_root(None) is None
        assert resolve_library_root(SimpleNamespace(volume_id=None)) is None
        lib = SimpleNamespace(volume_id="v1", volume=None)
        assert resolve_library_root(lib) is None

    def test_root_resolution(self):
        volume = SimpleNamespace(mount_path="/mnt/media/")
        lib = SimpleNamespace(volume_id="v1", volume=volume, root_subpath="/tv/")
        assert resolve_library_root(lib) == "/mnt/media/tv"
        lib.root_subpath = None
        assert resolve_library_root(lib) == "/mnt/media"

    def test_recycle_requires_subpath(self):
        assert resolve_library_recycle(None) is None
        lib = SimpleNamespace(recycle_subpath="")
        assert resolve_library_recycle(lib) is None

    def test_recycle_unbound_volume(self):
        lib = SimpleNamespace(recycle_subpath="recycle", volume_id=None)
        assert resolve_library_recycle(lib) is None
        lib = SimpleNamespace(recycle_subpath="recycle", volume_id="v1", volume=None)
        assert resolve_library_recycle(lib) is None

    def test_recycle_resolution(self):
        volume = SimpleNamespace(mount_path="/mnt/media/")
        lib = SimpleNamespace(
            recycle_subpath="/recycle/", volume_id="v1", volume=volume
        )
        assert resolve_library_recycle(lib) == "/mnt/media/recycle"


def test_check_mount(tmp_path):
    ok = check_mount(str(tmp_path))
    assert ok == {"exists": True, "readable": True, "writable": True}
    missing = check_mount(str(tmp_path / "nope"))
    assert missing == {"exists": False, "readable": False, "writable": False}


# ── text_normalizer ─────────────────────────────────────────────────────────


def test_normalize_title_opencc_unavailable(monkeypatch):
    """OpenCC import failure must degrade to NFKC+lowercase, not crash."""
    monkeypatch.setattr(tn, "_cc", None)
    monkeypatch.setattr(tn, "_cc_loaded", False)
    monkeypatch.setitem(sys.modules, "opencc", None)
    try:
        assert tn.normalize_title(" 進撃の巨人 ") == "進撃の巨人"
        assert tn._cc is None
    finally:
        # Reload the real OpenCC for subsequent callers.
        monkeypatch.setattr(tn, "_cc", None)
        monkeypatch.setattr(tn, "_cc_loaded", False)


def test_normalize_title_traditional_to_simplified():
    assert tn.normalize_title("進擊") == "进击"
    assert tn.normalize_title(None) == ""


def test_normalize_title_denoised_strips_brackets():
    # Bracket characters are stripped; their inner tokens remain as bare text.
    assert tn.normalize_title_denoised("[字幕组] 進擊的巨人 [1080p]") == "字幕组 进击的巨人 1080p"
    assert tn.normalize_title_denoised("") == ""
    assert tn.normalize_title_denoised(None) == ""


def test_similarity_score_paths():
    assert tn.similarity_score(None, "x") == 0
    assert tn.similarity_score("Same Title", "same  title") == 100
    # Single char vs longer title: no bigram overlap, Levenshtein only.
    assert tn.similarity_score("进", "进击的巨人") == 33
    # Levenshtein beats bigram Dice for near-identical Latin titles.
    assert tn.similarity_score("abcd", "abce") == 88


def test_bigram_edges():
    assert tn._bigrams("") == set()
    assert tn._bigrams("a") == {"a"}
    assert tn._bigrams("abc") == {"ab", "bc"}


def test_levenshtein_edges():
    assert tn._levenshtein("same", "same") == 0
    assert tn._levenshtein("", "ab") == 2
    assert tn._levenshtein("ab", "") == 2
    assert tn._levenshtein("kitten", "sitting") == 3


def test_levenshtein_ratio_edges():
    assert tn._levenshtein_ratio("", "x") == 0
    assert tn._levenshtein_ratio("x", "") == 0
    assert tn._levenshtein_ratio("same", "same") == 100
    # dist=1 over total 8 chars → 88.
    assert tn._levenshtein_ratio("abcd", "abce") == 88


def test_partial_similarity_edges():
    assert tn.partial_similarity_score("same", "same") == 100
    assert tn.partial_similarity_score("attack on titan", "attack on titan season 4") == 100
    # Sliding-window path: no containment, bigram overlap only.
    assert tn.partial_similarity_score("abcd", "abef") == 33
    assert tn.partial_similarity_score(None, "x") == 0
