"""Tests for the 429 rate-limit backoff added to app.parsers.validators.fetch_reddit_rss
and fetch_reddit_json: a 429 must sleep (honoring Retry-After when present, else a fixed
fallback) and then move on to the next domain/attempt, not be treated like a generic
non-200 failure."""
from unittest.mock import AsyncMock, patch

import pytest

from app.parsers.validators import (
    _REDDIT_RATE_LIMIT_BACKOFF_SECONDS,
    fetch_reddit_json,
    fetch_reddit_rss,
)
from tests.conftest import FakeSession, atom_feed


@pytest.mark.asyncio
async def test_rss_429_honors_retry_after_header_then_succeeds_on_next_domain():
    xml = atom_feed("")
    session = FakeSession([
        _FakeResponseWithHeaders(status=429, headers={"Retry-After": "3"}),
        _FakeResponseWithHeaders(status=200, text=xml, headers={"Content-Type": "application/atom+xml"}),
    ])
    with patch("app.parsers.validators.asyncio.sleep", AsyncMock()) as mock_sleep:
        result = await fetch_reddit_rss(session, "test")

    assert result == xml
    mock_sleep.assert_any_await(3.0)


@pytest.mark.asyncio
async def test_rss_429_without_retry_after_uses_fallback_backoff():
    xml = atom_feed("")
    session = FakeSession([
        _FakeResponseWithHeaders(status=429, headers={}),
        _FakeResponseWithHeaders(status=200, text=xml, headers={"Content-Type": "application/atom+xml"}),
    ])
    with patch("app.parsers.validators.asyncio.sleep", AsyncMock()) as mock_sleep:
        result = await fetch_reddit_rss(session, "test")

    assert result == xml
    mock_sleep.assert_any_await(_REDDIT_RATE_LIMIT_BACKOFF_SECONDS)


@pytest.mark.asyncio
async def test_json_429_honors_retry_after_header_then_succeeds_on_next_domain():
    payload = {"data": {"children": []}}
    session = FakeSession([
        _FakeResponseWithHeaders(status=429, headers={"Retry-After": "5"}),
        _FakeResponseWithHeaders(status=200, json_data=payload, headers={"Content-Type": "application/json"}),
    ])
    with patch("app.parsers.validators.asyncio.sleep", AsyncMock()) as mock_sleep:
        result = await fetch_reddit_json(session, "test")

    assert result == payload
    mock_sleep.assert_any_await(5.0)


class _FakeResponseWithHeaders:
    """Like conftest.FakeResponse but with a caller-supplied headers dict, needed to set
    Retry-After alongside/instead of Content-Type."""

    def __init__(self, status=200, text="", json_data=None, headers=None):
        self.status = status
        self._text = text
        self._json_data = json_data
        self.headers = headers or {}

    async def text(self):
        return self._text

    async def json(self):
        return self._json_data
