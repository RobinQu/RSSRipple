"""Tests for the is_anime tri-state flag: signals, upsert assignment, DSL.

Covers ``app.services.anime_signals`` (deterministic evidence rules),
``wikipedia_episode_parser.has_tvanime_infobox``, the metadata upsert
assignment semantics (True sticks, False fills NULL, never downgrade), and
the Filter DSL ``series.is_anime`` / ``movie.is_anime`` bool fields
(including the documented null semantics: positive ops fail, ``ne`` passes).
"""

from __future__ import annotations

import pathlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.services import metadata_service as ms
from app.services.anime_signals import (
    apply_is_anime,
    is_anime_from_tmdb,
    is_anime_identity,
)
from app.services.filter_engine import evaluate_field_condition, validate_filter_config
from app.services.wikipedia_episode_parser import has_tvanime_infobox

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "wikipedia"


# ---------------------------------------------------------------------------
# is_anime_from_tmdb — deterministic genre/language/country verdict
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "genre_ids,lang,countries,expected",
    [
        ([16, 10765], "ja", ["JP"], True),   # Animation + Japanese → anime
        ([16], "ja", [], True),              # language alone suffices
        ([16], None, ["JP"], True),          # country alone suffices
        ([18, 10759], "ja", ["JP"], False),  # no Animation → live-action
        ([16], "en", ["US"], None),          # Western animation → unknown
        ([], "ja", ["JP"], None),            # no genre data → unknown
        (None, "ja", None, None),
    ],
)
def test_is_anime_from_tmdb(genre_ids, lang, countries, expected):
    assert is_anime_from_tmdb(genre_ids, lang, countries) is expected


# ---------------------------------------------------------------------------
# is_anime_identity — bangumi/mal/anilist host anime only
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source,alt_ids,expected",
    [
        ("bangumi", None, True),
        ("mal", None, True),
        ("anilist", None, True),
        ("wikipedia", ["bangumi:123"], True),
        ("wikipedia", ["MAL:456"], True),
        ("tmdb", None, False),
        ("wikipedia", ["tmdb:82684", "imdb:tt1"], False),
        (None, None, False),
    ],
)
def test_is_anime_identity(source, alt_ids, expected):
    assert is_anime_identity(source, alt_ids) is expected


# ---------------------------------------------------------------------------
# apply_is_anime — True sticks, False fills NULL, never downgrade
# ---------------------------------------------------------------------------


def _work(is_anime=None):
    return SimpleNamespace(is_anime=is_anime)


def test_apply_is_anime_identity_wins():
    w = _work(is_anime=False)  # even a confirmed-False is upgraded by identity
    apply_is_anime(w, {"external_source": "bangumi"})
    assert w.is_anime is True


def test_apply_is_anime_true_sticks():
    w = _work()
    apply_is_anime(w, {"is_anime": True})
    assert w.is_anime is True
    apply_is_anime(w, {"is_anime": False})  # never downgrade
    assert w.is_anime is True


def test_apply_is_anime_false_only_fills_null():
    w = _work()
    apply_is_anime(w, {"is_anime": False})
    assert w.is_anime is False
    apply_is_anime(w, {})  # absent key keeps the value
    assert w.is_anime is False
    apply_is_anime(w, {"is_anime": True})  # False can still be upgraded
    assert w.is_anime is True


def test_apply_is_anime_absent_keeps_null():
    w = _work()
    apply_is_anime(w, {"external_source": "tmdb"})
    assert w.is_anime is None


# ---------------------------------------------------------------------------
# has_tvanime_infobox — deterministic Wikipedia anime signal
# ---------------------------------------------------------------------------


def test_has_tvanime_infobox_real_samples():
    assert has_tvanime_infobox((FIXTURES / "zh_100gf.wikitext").read_text()) is True
    # 史萊姆 main page: no TVAnime block (anime lives on a sub-page).
    assert has_tvanime_infobox((FIXTURES / "zh_slime.wikitext").read_text()) is False


def test_has_tvanime_infobox_rejects_novel_manga_decoys():
    wt = "{{Infobox animanga/Novel|話數=全677話}}{{Infobox animanga/Manga|話數=12}}"
    assert has_tvanime_infobox(wt) is False
    assert has_tvanime_infobox(None) is False
    assert has_tvanime_infobox("") is False


# ---------------------------------------------------------------------------
# Upsert integration — create_or_update_*_from_external
# ---------------------------------------------------------------------------


def _poster_patch():
    return patch(
        "app.services.metadata_service.download_and_cache_poster",
        new_callable=AsyncMock, return_value=None,
    )


async def test_upsert_series_is_anime_lifecycle(db_session):
    base = {
        "content_type": "tv",
        "title_cn": "剧A",
        "external_id": "tmdb:900001",
        "external_source": "tmdb",
    }
    with _poster_patch():
        s = await ms.create_or_update_series_from_external(
            db_session, {**base, "is_anime": False}
        )
    assert s.is_anime is False

    with _poster_patch():
        s = await ms.create_or_update_series_from_external(db_session, dict(base))
    assert s.is_anime is False  # absent key keeps the value

    with _poster_patch():
        s = await ms.create_or_update_series_from_external(
            db_session, {**base, "is_anime": True}
        )
    assert s.is_anime is True  # NULL/False can be upgraded

    with _poster_patch():
        s = await ms.create_or_update_series_from_external(
            db_session, {**base, "is_anime": False}
        )
    assert s.is_anime is True  # True is never downgraded


async def test_upsert_series_identity_marks_anime(db_session):
    data = {
        "content_type": "tv",
        "title_cn": "番剧B",
        "external_id": "mal:900002",
        "external_source": "mal",
        # no is_anime key — bangumi/mal/anilist identity is evidence enough
    }
    with _poster_patch():
        s = await ms.create_or_update_series_from_external(db_session, data)
    assert s.is_anime is True


async def test_upsert_movie_identity_marks_anime(db_session):
    data = {
        "content_type": "movie",
        "title_cn": "剧场版C",
        "external_id": "bangumi:900003",
        "external_source": "bangumi",
    }
    with _poster_patch():
        m = await ms.create_or_update_movie_from_external(db_session, data)
    assert m.is_anime is True


async def test_upsert_series_no_evidence_stays_null(db_session):
    data = {
        "content_type": "tv",
        "title_cn": "美剧D",
        "external_id": "tmdb:900004",
        "external_source": "tmdb",
    }
    with _poster_patch():
        s = await ms.create_or_update_series_from_external(db_session, data)
    assert s.is_anime is None


# ---------------------------------------------------------------------------
# Filter DSL — series.is_anime / movie.is_anime bool fields
# ---------------------------------------------------------------------------


def _res_with_series(is_anime):
    return SimpleNamespace(series=SimpleNamespace(is_anime=is_anime), movie=None)


def test_is_anime_fields_validate():
    assert validate_filter_config(
        {"field": "series.is_anime", "operator": "eq", "value": True}
    ) == []
    assert validate_filter_config(
        {"field": "movie.is_anime", "operator": "is_not_empty"}
    ) == []
    # String/number operators are rejected for bool fields.
    assert validate_filter_config(
        {"field": "series.is_anime", "operator": "contains", "value": "x"}
    ) != []
    assert validate_filter_config(
        {"field": "series.is_anime", "operator": "gt", "value": 1}
    ) != []


@pytest.mark.parametrize(
    "op,value,true_expected,false_expected,null_expected",
    [
        ("eq", True, True, False, False),
        ("eq", False, False, True, False),   # NULL fails positive ops
        ("ne", True, False, True, True),     # NULL passes ne
    ],
)
def test_is_anime_evaluate(op, value, true_expected, false_expected, null_expected):
    cond = {"field": "series.is_anime", "operator": op, "value": value}
    assert evaluate_field_condition(cond, _res_with_series(True)) is true_expected
    assert evaluate_field_condition(cond, _res_with_series(False)) is false_expected
    assert evaluate_field_condition(cond, _res_with_series(None)) is null_expected


def test_is_anime_emptiness_ops():
    cond = {"field": "series.is_anime", "operator": "is_empty"}
    assert evaluate_field_condition(cond, _res_with_series(None)) is True
    assert evaluate_field_condition(cond, _res_with_series(False)) is False
    cond = {"field": "series.is_anime", "operator": "is_not_empty"}
    assert evaluate_field_condition(cond, _res_with_series(True)) is True
    assert evaluate_field_condition(cond, _res_with_series(None)) is False


# ---------------------------------------------------------------------------
# bangumi_verdict — title/year-guarded match, type 2 → True / type 6 → False
# ---------------------------------------------------------------------------


from app.services.anime_signals import bangumi_verdict  # noqa: E402


def _subj(id, name, name_cn, type_, date="2023-10-01"):
    return {"id": id, "name": name, "name_cn": name_cn, "type": type_, "date": date}


def test_bangumi_verdict_anime_hit():
    verdict, subj = bangumi_verdict(
        ["葬送的芙莉莲", "Frieren"], 2023,
        [_subj(1, "葬送のフリーレン", "葬送的芙莉莲", 2)],
    )
    assert verdict is True
    assert subj["id"] == 1


def test_bangumi_verdict_live_action_hit():
    verdict, _ = bangumi_verdict(
        ["理智与情感"], 1995, [_subj(2, "理智与情感", "理智与情感", 6, "1995-12-13")]
    )
    assert verdict is False


def test_bangumi_verdict_year_guard_rejects_remake():
    # Same title but the subject is 8 years off the work's year → no match.
    verdict, _ = bangumi_verdict(
        ["攻壳机动队"], 2026, [_subj(3, "攻殻機動隊", "攻壳机动队", 2, "1995-11-18")]
    )
    assert verdict is None


def test_bangumi_verdict_unknown_year_passes():
    verdict, _ = bangumi_verdict(
        ["攻壳机动队"], None, [_subj(3, "攻殻機動隊", "攻壳机动队", 2, "1995-11-18")]
    )
    assert verdict is True


def test_bangumi_verdict_title_mismatch_and_other_types_ignored():
    verdict, _ = bangumi_verdict(
        ["芙莉莲"], 2023,
        [
            _subj(4, "不一样的作品", "不一样的作品", 2),          # title mismatch
            _subj(5, "葬送的芙莉莲", "葬送的芙莉莲", 1),          # book — ignored
        ],
    )
    assert verdict is None


def test_bangumi_verdict_alias_and_book_title_marks_match():
    # 《》marks / whitespace / trad-simp all normalize away; aliases count.
    verdict, _ = bangumi_verdict(
        [None, "Mushoku Tensei", "無職転生"], 2021,
        [_subj(6, "無職転生 ～異世界行ったら本気だす～", "", 2, "2021-01-10")],
    )
    assert verdict is None  # not equal — subtitle suffix differs
    verdict, _ = bangumi_verdict(
        ["無職転生～異世界行ったら本気だす～", "無職転生"], 2021,
        [_subj(6, "《無職転生 ～異世界行ったら本気だす～》", None, 2, "2021-01-10")],
    )
    assert verdict is True
