"""Tests for the low-level Reddit RSS/JSON fetch helpers in app.parsers.validators, covering
the content-type/content guards that are supposed to reject blocked/challenge responses, and
the www->old.reddit.com cross-domain + 2-attempt retry behavior."""
import pytest

from app.parsers import validators
from tests.conftest import FakeResponse, FakeSession


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch):
    # Avoid real sleeps in the retry loop; only the *presence* of the retry matters here.
    monkeypatch.setattr(validators, "_REDDIT_RETRY_DELAY_SECONDS", 0)
    monkeypatch.setattr(validators, "_REDDIT_RATE_LIMIT_BACKOFF_SECONDS", 0)


@pytest.mark.asyncio
async def test_fetch_reddit_rss_returns_text_on_success():
    session = FakeSession([FakeResponse(status=200, text="<feed><entry></entry></feed>")])
    result = await validators.fetch_reddit_rss(session, "test")
    assert result == "<feed><entry></entry></feed>"


@pytest.mark.asyncio
async def test_fetch_reddit_rss_rejects_html_challenge_page():
    # HTTP 200 but body is an HTML challenge/block page, not real RSS/Atom -> the "<feed"/"<rss"
    # content guard must reject it on every domain/attempt, ending in None (not the raw HTML).
    html_page = "<html><head><title>Just a moment...</title></head><body>Checking your browser</body></html>"
    responses = [FakeResponse(status=200, text=html_page) for _ in range(4)]  # 2 domains x 2 attempts
    session = FakeSession(responses)
    result = await validators.fetch_reddit_rss(session, "test")
    assert result is None


@pytest.mark.asyncio
async def test_fetch_reddit_rss_falls_back_from_www_429_to_old_reddit_success():
    session = FakeSession([
        FakeResponse(status=429, text=""),
        FakeResponse(status=200, text="<feed><entry></entry></feed>"),
    ])
    result = await validators.fetch_reddit_rss(session, "test")
    assert result == "<feed><entry></entry></feed>"
    assert "www.reddit.com" in session.requested_urls[0]
    assert "old.reddit.com" in session.requested_urls[1]


@pytest.mark.asyncio
async def test_fetch_reddit_rss_retries_second_attempt_after_both_domains_fail_once():
    session = FakeSession([
        FakeResponse(status=503, text=""),
        FakeResponse(status=503, text=""),
        FakeResponse(status=200, text="<feed></feed>"),
    ])
    result = await validators.fetch_reddit_rss(session, "test")
    assert result == "<feed></feed>"
    assert len(session.requested_urls) == 3


@pytest.mark.asyncio
async def test_fetch_reddit_rss_network_exception_does_not_propagate():
    session = FakeSession([
        ConnectionError("simulated network failure"),
        FakeResponse(status=200, text="<feed></feed>"),
    ])
    result = await validators.fetch_reddit_rss(session, "test")
    assert result == "<feed></feed>"


@pytest.mark.asyncio
async def test_fetch_reddit_rss_all_attempts_exhausted_returns_none():
    session = FakeSession([FakeResponse(status=500, text="") for _ in range(4)])
    result = await validators.fetch_reddit_rss(session, "test")
    assert result is None


@pytest.mark.asyncio
async def test_fetch_reddit_json_rejects_non_json_content_type():
    html_page = "<html>blocked</html>"
    responses = [FakeResponse(status=200, text=html_page, content_type="text/html") for _ in range(4)]
    session = FakeSession(responses)
    result = await validators.fetch_reddit_json(session, "test")
    assert result is None


@pytest.mark.asyncio
async def test_fetch_reddit_json_returns_parsed_json_on_success():
    payload = {"data": {"children": []}}
    session = FakeSession([FakeResponse(status=200, json_data=payload, content_type="application/json")])
    result = await validators.fetch_reddit_json(session, "test")
    assert result == payload


@pytest.mark.asyncio
async def test_reddit_subreddit_exists_true_via_rss_without_calling_json():
    session = FakeSession([FakeResponse(status=200, text="<feed></feed>")])
    exists = await validators._reddit_subreddit_exists(session, "test")
    assert exists is True
    assert len(session.requested_urls) == 1  # only the RSS call happened


@pytest.mark.asyncio
async def test_reddit_subreddit_exists_falls_back_to_json_true():
    session = FakeSession(
        [FakeResponse(status=500, text="") for _ in range(4)]  # RSS exhausted
        + [FakeResponse(status=200, json_data={"data": {"children": []}}, content_type="application/json")]
    )
    exists = await validators._reddit_subreddit_exists(session, "test")
    assert exists is True


@pytest.mark.asyncio
async def test_reddit_subreddit_exists_false_when_both_fail():
    session = FakeSession([FakeResponse(status=500, text="") for _ in range(8)])
    exists = await validators._reddit_subreddit_exists(session, "test")
    assert exists is False
