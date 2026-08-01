"""Tests for Wikipedia category-based content-type inference.

Regression: the franchise page of "關於我轉生變成史萊姆這檔事" carries both
"2021年日本電視動畫" (TV anime) and "2022年日本動畫電影" (the Scarlet Bond
film) categories. A film-keyword-only check flipped the whole TV series to a
Movie record; TV indicators must win on mixed franchise pages.
"""
from app.services.metadata_wiki_classify import _infer_content_type_from_categories


def test_tv_anime_categories():
    assert _infer_content_type_from_categories(
        ["2018年日本電視動畫", "改編自輕小說的動畫"]
    ) == "tv"


def test_film_only_categories():
    assert _infer_content_type_from_categories(
        ["2022年日本動畫電影", "Japanese animated films"]
    ) == "movie"


def test_franchise_page_with_both_tv_and_film_prefers_tv():
    # Real category mix from zhwiki page 5139056 (Slime franchise).
    assert _infer_content_type_from_categories(
        [
            "2018年日本電視動畫",
            "2021年日本電視動畫",
            "2022年日本動畫電影",
            "日本奇幻小說",
            "日本漫畫作品",
        ]
    ) == "tv"


def test_studio_name_with_films_does_not_flip_tv_series():
    # Regression: "黑貓與魔女的教室" (zhwiki 8154341) was misjudged as movie
    # because the studio category "Liden Films" contains "films".
    assert _infer_content_type_from_categories(
        [
            "2022年日本漫畫作品",
            "2026年日本電視動畫",
            "Liden Films",
            "奇幻動畫",
            "改編自漫畫的動畫",
            "播放中的動畫",
        ]
    ) == "tv"


def test_english_franchise_page_prefers_tv():
    assert _infer_content_type_from_categories(
        ["Japanese television series", "Japanese animated films"]
    ) == "tv"


def test_japanese_tv_anime_keyword():
    assert _infer_content_type_from_categories(["テレビアニメ"]) == "tv"


def test_japanese_film_only_keyword():
    assert _infer_content_type_from_categories(["アニメ映画"]) == "movie"


def test_no_signal_defaults_to_tv():
    assert _infer_content_type_from_categories(["8bit", "月刊少年天狼星"]) == "tv"
    assert _infer_content_type_from_categories([]) == "tv"
