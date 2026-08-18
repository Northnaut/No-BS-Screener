"""Tests for app.parsers.reddit.fetch_reddit_posts: the RSS-primary/JSON-fallback orchestration."""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.parsers.reddit import fetch_reddit_posts
from tests.conftest import atom_entry, atom_feed


@pytest.mark.asyncio
async def test_rss_success_skips_json_entirely():
    xml = atom_feed(atom_entry(entry_id="t3_rss_post"))
    with patch("app.parsers.reddit.fetch_reddit_rss", AsyncMock(return_value=xml)), \
         patch("app.parsers.reddit.fetch_reddit_json", AsyncMock()) as mock_json:
        posts = await fetch_reddit_posts(session=None, subreddit="test", limit=25)

    assert len(posts) == 1
    assert posts[0].external_id == "t3_rss_post"
    mock_json.assert_not_called()


@pytest.mark.asyncio
async def test_rss_failure_falls_back_to_json_success():
    json_payload = {
        "data": {
            "children": [
                {
                    "data": {
                        "name": "t3_json_post",
                        "title": "From JSON",
                        "permalink": "/r/test/comments/json_post/",
                        "created_utc": 1700000000,
                        "selftext": "json body",
                    }
                }
            ]
        }
    }
    with patch("app.parsers.reddit.fetch_reddit_rss", AsyncMock(return_value=None)), \
         patch("app.parsers.reddit.fetch_reddit_json", AsyncMock(return_value=json_payload)):
        posts = await fetch_reddit_posts(session=None, subreddit="test", limit=25)

    assert len(posts) == 1
    assert posts[0].external_id == "t3_json_post"
    assert posts[0].title == "From JSON"
    assert posts[0].text == "json body"
    assert posts[0].url == "https://www.reddit.com/r/test/comments/json_post/"
    assert posts[0].published_at == datetime.fromtimestamp(1700000000, tz=timezone.utc)


@pytest.mark.asyncio
async def test_rss_and_json_both_fail_raises_runtime_error():
    with patch("app.parsers.reddit.fetch_reddit_rss", AsyncMock(return_value=None)), \
         patch("app.parsers.reddit.fetch_reddit_json", AsyncMock(return_value=None)):
        with pytest.raises(RuntimeError, match="RSS and JSON both failed"):
            await fetch_reddit_posts(session=None, subreddit="test", limit=25)


@pytest.mark.asyncio
async def test_json_fallback_skips_malformed_children_keeps_valid_ones():
    json_payload = {
        "data": {
            "children": [
                {"data": {"title": "missing name/permalink/created_utc"}},
                {
                    "data": {
                        "name": "t3_valid",
                        "title": "Valid",
                        "permalink": "/r/test/comments/valid/",
                        "created_utc": 1700000000,
                    }
                },
            ]
        }
    }
    with patch("app.parsers.reddit.fetch_reddit_rss", AsyncMock(return_value=None)), \
         patch("app.parsers.reddit.fetch_reddit_json", AsyncMock(return_value=json_payload)):
        posts = await fetch_reddit_posts(session=None, subreddit="test", limit=25)

    assert len(posts) == 1
    assert posts[0].external_id == "t3_valid"
    # selftext defaults to "" when absent (link/image posts).
    assert posts[0].text == ""


@pytest.mark.asyncio
async def test_json_fallback_with_unexpected_shape_raises_wrapped_runtime_error():
    # `children` items that aren't dicts should be caught by the outer try/except and re-raised
    # as a RuntimeError with context, not an unrelated AttributeError/TypeError leaking out.
    json_payload = {"data": {"children": ["not-a-dict"]}}
    with patch("app.parsers.reddit.fetch_reddit_rss", AsyncMock(return_value=None)), \
         patch("app.parsers.reddit.fetch_reddit_json", AsyncMock(return_value=json_payload)):
        with pytest.raises(RuntimeError, match="Failed to parse JSON fallback response"):
            await fetch_reddit_posts(session=None, subreddit="test", limit=25)


@pytest.mark.asyncio
async def test_zero_entries_rss_feed_does_not_trigger_json_fallback():
    xml = atom_feed("")
    with patch("app.parsers.reddit.fetch_reddit_rss", AsyncMock(return_value=xml)), \
         patch("app.parsers.reddit.fetch_reddit_json", AsyncMock()) as mock_json:
        posts = await fetch_reddit_posts(session=None, subreddit="test", limit=25)

    assert posts == []
    mock_json.assert_not_called()
