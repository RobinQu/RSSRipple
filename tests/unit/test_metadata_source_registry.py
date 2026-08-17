"""Tests for metadata_source_registry: the 7-site identity authority."""

import pytest

from app.services import metadata_source_registry as reg

# ---------------------------------------------------------------------------
# canonicalize_external_id
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw_id", "source", "expected"),
    [
        ("TMDB:82684", None, "tmdb:82684"),
        ("TMDB 82684", None, "tmdb:82684"),
        ("TMDB TV 82684 / season 4", None, "tmdb:82684"),
        ("82684", "tmdb", "tmdb:82684"),
        ("TMDB:123 / Season 2", None, "tmdb:123"),
        ("tt0944947", None, "imdb:tt0944947"),
        ("imdb:tt0944947", None, "imdb:tt0944947"),
        # Canonical "source:id" forms of the other 5 registry sites pass through.
        ("bangumi:12345", None, "bangumi:12345"),
        ("MAL:5114", None, "mal:5114"),
        ("anilist:21", None, "anilist:21"),
        ("douban:1292052", None, "douban:1292052"),
        ("wikipedia:7727654", None, "wikipedia:7727654"),
        (None, None, None),
        ("", None, None),
    ],
)
def test_canonicalize_external_id(raw_id, source, expected):
    assert reg.canonicalize_external_id(raw_id, source) == expected


# ---------------------------------------------------------------------------
# source_and_id_from_url
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://bangumi.tv/subject/12345", ("bangumi", "bangumi:12345")),
        ("https://bgm.tv/subject/7", ("bangumi", "bangumi:7")),
        ("https://www.themoviedb.org/tv/999", ("tmdb", "tmdb:999")),
        ("https://www.themoviedb.org/movie/550", ("tmdb", "tmdb:550")),
        ("https://myanimelist.net/anime/5114", ("mal", "mal:5114")),
        ("https://anilist.co/anime/21", ("anilist", "anilist:21")),
        ("https://www.imdb.com/title/tt0944947/", ("imdb", "imdb:tt0944947")),
        ("https://movie.douban.com/subject/1292052", ("douban", "douban:1292052")),
        ("https://zh.wikipedia.org/wiki/進撃的巨人", ("wikipedia", "wikipedia:進撃的巨人")),
    ],
)
def test_source_and_id_from_authoritative_urls(url, expected):
    assert reg.source_and_id_from_url(url) == expected


def test_source_and_id_from_url_dropped_sites_and_unknown():
    # baidu_baike / eiga are dropped from the identity scheme.
    assert reg.source_and_id_from_url("https://baike.baidu.com/item/测试剧集/12345") is None
    assert reg.source_and_id_from_url("https://eiga.com/movie/12345") is None
    assert reg.source_and_id_from_url("https://example.com/page") is None
    # Known host but no id pattern in path.
    assert reg.source_and_id_from_url("https://bangumi.tv/") is None


# ---------------------------------------------------------------------------
# build_source_links
# ---------------------------------------------------------------------------


def _urls(links):
    return [(link["source"], link["label"], link["url"]) for link in links]


def test_build_source_links_canonical_tmdb_tv_and_movie():
    links = reg.build_source_links("tmdb:82684", "tmdb", "tv")
    assert _urls(links) == [("tmdb", "TMDB", "https://www.themoviedb.org/tv/82684")]
    links = reg.build_source_links("tmdb:550", "tmdb", "movie")
    assert _urls(links) == [("tmdb", "TMDB", "https://www.themoviedb.org/movie/550")]


def test_build_source_links_legacy_compound_split():
    links = reg.build_source_links("TMDB:632617; IMDb:tt10986222", "llm_search", "tv")
    assert _urls(links) == [
        ("tmdb", "TMDB", "https://www.themoviedb.org/tv/632617"),
        ("imdb", "IMDb", "https://www.imdb.com/title/tt10986222/"),
    ]


def test_build_source_links_bare_imdb_and_bare_tmdb_with_declared_source():
    links = reg.build_source_links("tt0944947", "imdb", "tv")
    assert _urls(links) == [("imdb", "IMDb", "https://www.imdb.com/title/tt0944947/")]
    links = reg.build_source_links("632617", "tmdb", "movie")
    assert _urls(links) == [("tmdb", "TMDB", "https://www.themoviedb.org/movie/632617")]


def test_build_source_links_wikipedia_url_and_numeric_page_id():
    wiki = "https://zh.wikipedia.org/wiki/黃泉使者"
    links = reg.build_source_links("wikipedia:7727654", "wikipedia", "tv", wikipedia_url=wiki)
    # wikipedia_url wins; the numeric page id is deduped away, and the label
    # carries the edition parsed from the URL host.
    assert _urls(links) == [("wikipedia", "Wikipedia (zh)", wiki)]

    links = reg.build_source_links("wikipedia:7727654", "wikipedia", "tv")
    assert _urls(links) == [
        ("wikipedia", "Wikipedia", "https://en.wikipedia.org/?curid=7727654")
    ]


def test_build_source_links_other_registry_sites():
    links = reg.build_source_links("bangumi:12345", "bangumi", "tv")
    assert _urls(links) == [("bangumi", "Bangumi", "https://bangumi.tv/subject/12345")]
    links = reg.build_source_links("mal:5114|anilist:21|douban:1292052", None, "tv")
    assert _urls(links) == [
        ("mal", "MyAnimeList", "https://myanimelist.net/anime/5114"),
        ("anilist", "AniList", "https://anilist.co/anime/21"),
        ("douban", "豆瓣", "https://movie.douban.com/subject/1292052/"),
    ]


def test_build_source_links_empty_and_dedupe():
    assert reg.build_source_links(None, None, "tv") == []
    assert reg.build_source_links("exa_web", "exa_web", "tv") == []
    # Same link twice (compound + canonical) collapses.
    links = reg.build_source_links("tmdb:82684; tmdb:82684", "tmdb", "tv")
    assert len(links) == 1


# ---------------------------------------------------------------------------
# Wikipedia language-qualified ids (wikipedia:{lang}:{pageid})
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("external_id", "expected"),
    [
        ("wikipedia:zh:7301786", ("zh", "7301786")),
        ("wikipedia:en:65944845", ("en", "65944845")),
        ("wikipedia:7301786", (None, "7301786")),  # legacy bare form
        ("wikipedia:進撃の巨人", (None, None)),  # slug form
        ("tmdb:82684", (None, None)),
        (None, (None, None)),
        ("", (None, None)),
    ],
)
def test_parse_wikipedia_id(external_id, expected):
    assert reg.parse_wikipedia_id(external_id) == expected


def test_qualify_wikipedia_id():
    # Explicit lang wins.
    assert reg.qualify_wikipedia_id("wikipedia:7301786", lang="zh") == "wikipedia:zh:7301786"
    # Fall back to the wikipedia_url host's edition.
    assert reg.qualify_wikipedia_id(
        "wikipedia:7301786", wikipedia_url="https://ja.wikipedia.org/wiki/X"
    ) == "wikipedia:ja:7301786"
    # Already qualified / unknown edition / slug / non-wikipedia pass through.
    assert reg.qualify_wikipedia_id("wikipedia:zh:7301786", lang="en") == "wikipedia:zh:7301786"
    assert reg.qualify_wikipedia_id("wikipedia:7301786") == "wikipedia:7301786"
    assert reg.qualify_wikipedia_id("wikipedia:Some_Title", lang="en") == "wikipedia:some_title"
    assert reg.qualify_wikipedia_id("tmdb:82684", lang="en") == "tmdb:82684"
    assert reg.qualify_wikipedia_id(None) is None


def test_wikipedia_match_keys():
    keys, like = reg.wikipedia_match_keys("wikipedia:zh:7301786")
    assert keys == ["wikipedia:zh:7301786", "wikipedia:7301786"]
    assert like is None

    keys, like = reg.wikipedia_match_keys("wikipedia:7301786")
    assert keys == ["wikipedia:7301786"]
    assert like == "wikipedia:%:7301786"

    keys, like = reg.wikipedia_match_keys("tmdb:82684")
    assert keys == ["tmdb:82684"]
    assert like is None


def test_build_source_links_qualified_wikipedia_ids_per_edition():
    links = reg.build_source_links("wikipedia:zh:7301786", "wikipedia", "tv")
    assert _urls(links) == [
        ("wikipedia", "Wikipedia (zh)", "https://zh.wikipedia.org/?curid=7301786")
    ]
    # Bag ids in several editions each get their own labelled link.
    links = reg.build_source_links(
        "wikipedia:zh:7301786", "wikipedia", "tv",
        extra_ids=["wikipedia:en:65944845", "wikipedia:ja:4053941", "bangumi:12345"],
    )
    assert _urls(links) == [
        ("wikipedia", "Wikipedia (zh)", "https://zh.wikipedia.org/?curid=7301786"),
        ("wikipedia", "Wikipedia (en)", "https://en.wikipedia.org/?curid=65944845"),
        ("wikipedia", "Wikipedia (ja)", "https://ja.wikipedia.org/?curid=4053941"),
        ("bangumi", "Bangumi", "https://bangumi.tv/subject/12345"),
    ]


def test_build_source_links_legacy_bare_wikipedia_bag_id():
    # Legacy bare bag ids keep the historical en-edition curid rendering.
    links = reg.build_source_links(
        "wikipedia:zh:7301786", "wikipedia", "tv", extra_ids=["wikipedia:4053941"]
    )
    assert _urls(links) == [
        ("wikipedia", "Wikipedia (zh)", "https://zh.wikipedia.org/?curid=7301786"),
        ("wikipedia", "Wikipedia", "https://en.wikipedia.org/?curid=4053941"),
    ]
