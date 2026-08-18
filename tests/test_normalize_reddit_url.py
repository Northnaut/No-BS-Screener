import pytest

from app.parsers.reddit import _normalize_reddit_url


@pytest.mark.parametrize("domain", ["old", "new", "np", "amp"])
def test_alternate_domains_are_rewritten_to_www(domain):
    url = f"https://{domain}.reddit.com/r/test/comments/abc123/some_title/"
    result = _normalize_reddit_url(url)
    assert result == "https://www.reddit.com/r/test/comments/abc123/some_title/"


def test_www_domain_is_left_unchanged():
    url = "https://www.reddit.com/r/test/comments/abc123/some_title/"
    assert _normalize_reddit_url(url) == url


def test_non_reddit_domain_is_left_unchanged():
    url = "https://example.com/r/test/comments/abc123/some_title/"
    assert _normalize_reddit_url(url) == url


def test_empty_string_returns_empty_string():
    assert _normalize_reddit_url("") == ""


def test_http_scheme_is_also_rewritten():
    url = "http://old.reddit.com/r/test/comments/abc123/some_title/"
    assert _normalize_reddit_url(url) == "http://www.reddit.com/r/test/comments/abc123/some_title/"
