"""Tests for text_normalizer: NFKC/OpenCC normalization and similarity scores."""

from app.services import text_normalizer as tn


def test_normalize_title_nfkc_lowercase_and_whitespace():
    # Full-width Latin -> ASCII, upper -> lower, whitespace collapsed
    assert tn.normalize_title("Ｆｕｌｌ　Width  SHOW ") == "full width show"


def test_normalize_title_traditional_to_simplified():
    assert tn.normalize_title("測試劇集") == "测试剧集"


def test_normalize_title_empty_and_none():
    assert tn.normalize_title("") == ""
    assert tn.normalize_title(None) == ""


def test_normalize_title_denoised_strips_brackets():
    # Only the bracket characters are removed; their content is kept.
    assert tn.normalize_title_denoised("[VCB-Studio] 测试剧集 [01][1080p]") == "vcb-studio 测试剧集 01 1080p"


def test_normalize_title_denoised_empty():
    assert tn.normalize_title_denoised("") == ""
    assert tn.normalize_title_denoised(None) == ""


def test_bigrams_edge_cases():
    assert tn._bigrams("") == set()
    assert tn._bigrams("a") == {"a"}
    assert tn._bigrams("abc") == {"ab", "bc"}


def test_levenshtein_basic():
    assert tn._levenshtein("abc", "abc") == 0
    assert tn._levenshtein("", "abc") == 3
    assert tn._levenshtein("abc", "") == 3
    assert tn._levenshtein("kitten", "sitting") == 3


def test_levenshtein_ratio():
    assert tn._levenshtein_ratio("", "abc") == 0
    assert tn._levenshtein_ratio("abc", "abc") == 100
    assert tn._levenshtein_ratio("abcd", "abce") == round(100 * (8 - 1) / 8)


def test_similarity_score_exact_and_empty():
    assert tn.similarity_score("测试剧集", "測試劇集") == 100  # trad/simp converge
    assert tn.similarity_score("", "x") == 0
    assert tn.similarity_score(None, None) == 0


def test_similarity_score_substring_overlap():
    score = tn.similarity_score("测试剧集", "测试剧集第二季")
    assert 50 < score < 100
    assert tn.similarity_score(" completely different ", "xyz") < 30


def test_partial_similarity_score():
    assert tn.partial_similarity_score("", "abc") == 0
    assert tn.partial_similarity_score("abc", "abc") == 100
    # Containment -> 100
    assert tn.partial_similarity_score("Attack on Titan", "Attack on Titan Season 4 Part 2") == 100
    # Partial bigram overlap between non-contained strings
    score = tn.partial_similarity_score("测试剧集番外", "测试剧集正片")
    assert 0 < score <= 100


def test_opencc_failure_falls_back_to_plain_normalize(monkeypatch):
    """When OpenCC cannot be constructed, normalize_title still works."""

    class _Boom:
        def __init__(self, *a, **k):
            raise RuntimeError("no opencc data")

    monkeypatch.setattr(tn, "_cc_loaded", False)
    monkeypatch.setattr(tn, "_cc", None)
    monkeypatch.setattr("opencc.OpenCC", _Boom)

    assert tn._get_cc() is None
    # NFKC + lowercase still applied without the OpenCC step
    assert tn.normalize_title("ＴＥＳＴ") == "test"
