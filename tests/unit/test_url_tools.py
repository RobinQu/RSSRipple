"""Tests for url_tools: URL normalization and per-host diversity capping."""

from app.services.url_tools import keep_k_per_hostname, normalize_url


def test_normalize_url_strips_tracking_params_and_unifies_scheme():
    url = "HTTP://Example.COM/path/?utm_source=google&id=42&utm_medium=cpc#section"
    assert normalize_url(url) == "https://example.com/path?id=42"


def test_normalize_url_keeps_non_tracking_params_in_order():
    assert normalize_url("https://a.com/p?b=2&a=1") == "https://a.com/p?b=2&a=1"


def test_normalize_url_tracking_param_match_is_case_insensitive():
    assert normalize_url("https://a.com/p?UTM_SOURCE=x&keep=1") == "https://a.com/p?keep=1"


def test_normalize_url_trailing_slash_and_root():
    assert normalize_url("https://a.com/foo/") == "https://a.com/foo"
    assert normalize_url("https://a.com") == "https://a.com/"


def test_normalize_url_rejects_empty_and_non_string():
    assert normalize_url("") is None
    assert normalize_url(None) is None
    assert normalize_url(123) is None
    assert normalize_url("   ") is None


def test_normalize_url_rejects_relative_urls():
    assert normalize_url("/just/a/path") is None
    assert normalize_url("not a url at all") is None


def test_keep_k_per_hostname_caps_per_host_preserving_order():
    items = [
        {"url": "https://a.com/1"},
        {"url": "https://b.com/1"},
        {"url": "https://a.com/2"},
        {"url": "https://a.com/3"},  # over the k=2 cap for a.com
        {"link": "https://b.com/2"},  # link field works too
    ]
    out = keep_k_per_hostname(items, k=2)
    assert [i.get("url") or i.get("link") for i in out] == [
        "https://a.com/1",
        "https://b.com/1",
        "https://a.com/2",
        "https://b.com/2",
    ]


def test_keep_k_per_hostname_handles_empty_and_malformed_items():
    assert keep_k_per_hostname([]) == []
    items = [{"url": "https://a.com/1"}, "not-a-dict", {}, {"url": ""}]
    out = keep_k_per_hostname(items)
    # non-dict skipped; items without a host are never capped
    assert out == [{"url": "https://a.com/1"}, {}, {"url": ""}]
