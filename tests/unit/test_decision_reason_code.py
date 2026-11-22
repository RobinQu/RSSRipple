"""decision_reason_code / decision_reason_payload: UI-localizable decision reasons."""

from types import SimpleNamespace

from app.services.decision_store import (
    choice_identity,
    decision_reason_code,
    decision_reason_payload,
)


def _scope(kind, target_id="w1", season=None, episode=None, coverage=None):
    return choice_identity(kind, target_id, season, episode, coverage)[1]


def test_legacy_scope_less_decision_keeps_stored_text():
    assert decision_reason_code(None) is None
    assert decision_reason_code("v1:abc") is None
    assert decision_reason_code({}) is None


def test_series_episode_variants():
    assert decision_reason_code(_scope("series", season=1, episode=3)) == "series_season_episode"
    assert decision_reason_code(_scope("series", episode=3)) == "series_episode"
    assert decision_reason_code(_scope("series")) == "series"


def test_movie_and_batch_codes():
    assert decision_reason_code(_scope("movie")) == "movie"
    movie_cov = (("movie", "m1", None, ()),)
    assert decision_reason_code(_scope(None, None, None, -1, coverage=movie_cov)) == "batch_movie"
    season_cov = (("series", "s1", 1, ((1, 12),)),)
    assert decision_reason_code(_scope(None, None, None, -1, coverage=season_cov)) == "batch_season"
    multi_cov = (("series", "s1", 1, ((1, 12),)), ("series", "s2", 2, ((1, 12),)))
    assert decision_reason_code(_scope(None, None, None, -1, coverage=multi_cov)) == "batch_multi_season"


def test_malformed_coverage_is_not_a_code():
    assert decision_reason_code({"kind": "batch", "coverage": None}) is None
    assert decision_reason_code({"kind": "batch", "coverage": []}) is None
    assert decision_reason_code({"kind": "batch", "coverage": [["series"]]}) is None
    assert decision_reason_code({"kind": "unknown"}) is None


def test_reason_payload_resolves_title_and_params():
    _, scope = choice_identity("series", "w1", 2, 5)
    decision = SimpleNamespace(
        decision_scope=scope, season=2, episode=5,
        series=SimpleNamespace(title_cn="中文名", title_en="English"),
        movie=None,
    )
    assert decision_reason_payload(decision) == {
        "reason_code": "series_season_episode",
        "reason_params": {"title": "中文名", "season": 2, "episode": 5},
    }


def test_reason_payload_without_scope_is_passthrough():
    decision = SimpleNamespace(decision_scope=None, season=None, episode=None)
    assert decision_reason_payload(decision) == {
        "reason_code": None,
        "reason_params": None,
    }
