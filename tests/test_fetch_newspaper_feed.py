"""Tests for app.parsers.newspapers.fetch_newspaper_feed and its two small helpers,
_clean_html and _parse_feed's error handling. `_fetch_feed_text` (the network call) is
mocked at the module level it's imported into; feedparser itself parses real synthetic
RSS 2.0 XML strings, exactly like the production code path (feedparser.parse() on raw
text) - mirroring tests/test_fetch_via_rss.py's approach for the Reddit parser.

Some fallback branches (id -> link, summary -> description, published_parsed ->
updated_parsed -> now()) are exercised via a hand-built fake feed object patched in at
_parse_feed instead, since feedparser normalizes/aliases several of these RSS 2.0 fields
in ways that make it impossible to independently trigger each branch through real XML
alone (e.g. <description> is aliased to both entry['summary'] and entry['description']
by feedparser, so you can never observe "summary missing but description present" via
real XML - only entry.get(...)'s OR logic itself can be probed directly this way)."""
from unittest.mock import AsyncMock, patch

import pytest

from app.parsers.newspapers import _clean_html, fetch_newspaper_feed
from tests.conftest import rss_feed, rss_item


def _mock_feed_text(xml_text):
    return patch("app.parsers.newspapers._fetch_feed_text", AsyncMock(return_value=xml_text))


# ------------------------------ _clean_html ------------------------------

def test_clean_html_strips_tags_and_collapses_whitespace():
    assert _clean_html("<p>Hello   <b>world</b></p>\n\n<p>Line two</p>") == "Hello world Line two"


def test_clean_html_empty_string_returns_empty():
    assert _clean_html("") == ""


def test_clean_html_none_returns_empty():
    assert _clean_html(None) == ""


def test_clean_html_plain_text_without_tags_is_unchanged():
    assert _clean_html("Just plain text.") == "Just plain text."


def test_clean_html_nested_tags_leave_a_separating_space():
    # A naive tag-strip without a separator would glue "Marketcrashes" together.
    result = _clean_html("<div><span>Market</span><span>crashes</span></div>")
    assert "Market" in result and "crashes" in result
    assert "Marketcrashes" not in result


# --------------------------- fetch_newspaper_feed (via real feedparser) ---------------------------

@pytest.mark.asyncio
async def test_normal_feed_returns_parsed_posts():
    xml = rss_feed(
        rss_item(guid="g1", title="First Story", link="https://example.com/1",
                  description="<p>Body one</p>", pub_date="Wed, 02 Oct 2024 15:00:00 +0000")
        + rss_item(guid="g2", title="Second Story", link="https://example.com/2",
                    description="Body two", pub_date="Thu, 03 Oct 2024 09:30:00 +0000")
    )
    with _mock_feed_text(xml):
        posts = await fetch_newspaper_feed(session=None, title="Test Paper", url="https://example.com/feed")

    assert len(posts) == 2
    assert posts[0].external_id == "g1"
    assert posts[0].title == "First Story"
    assert posts[0].text == "Body one"
    assert posts[0].url == "https://example.com/1"
    assert posts[0].published_at.year == 2024
    assert posts[0].published_at.month == 10
    assert posts[0].published_at.day == 2
    assert posts[1].external_id == "g2"
    assert posts[1].text == "Body two"


@pytest.mark.asyncio
async def test_network_fetch_failure_returns_empty_list_not_raise():
    with _mock_feed_text(None):
        posts = await fetch_newspaper_feed(session=None, title="Test Paper", url="https://example.com/feed")
    assert posts == []


@pytest.mark.asyncio
async def test_totally_empty_feed_returns_empty_list():
    xml = rss_feed("")
    with _mock_feed_text(xml):
        posts = await fetch_newspaper_feed(session=None, title="Test Paper", url="https://example.com/feed")
    assert posts == []


@pytest.mark.asyncio
async def test_non_feed_garbage_response_returns_empty_list_gracefully():
    # A completely non-XML response (e.g. an API error, a proxy's plaintext error message)
    # makes feedparser set bozo=True with zero recovered entries - the early-return branch.
    garbage = "this is not xml or a feed at all <<<>>> {}"
    with _mock_feed_text(garbage):
        posts = await fetch_newspaper_feed(session=None, title="Test Paper", url="https://example.com/feed")
    assert posts == []


@pytest.mark.asyncio
async def test_html_error_page_response_returns_empty_list_gracefully():
    # A well-formed-enough HTML error/interstitial page (e.g. a CDN block page) parses with
    # bozo=False but zero entries - falls through the entries loop instead, still empty, still
    # no crash.
    html_page = "<html><head><title>404 Not Found</title></head><body>Nothing here</body></html>"
    with _mock_feed_text(html_page):
        posts = await fetch_newspaper_feed(session=None, title="Test Paper", url="https://example.com/feed")
    assert posts == []


@pytest.mark.asyncio
async def test_entry_missing_link_is_skipped_others_kept():
    # isPermaLink="false" is required here - per the RSS 2.0 spec, a <guid> defaults to
    # isPermaLink="true", and feedparser then backfills entry.link from the guid when <link>
    # is absent (RSS's equivalent of Atom's guidislink quirk, see conftest.rss_item's
    # docstring), so this entry would NOT actually end up linkless without it.
    xml = rss_feed(
        rss_item(guid="no-link", title="No link here", include_link=False, guid_is_permalink=False)
        + rss_item(guid="good", title="Good entry", link="https://example.com/good")
    )
    with _mock_feed_text(xml):
        posts = await fetch_newspaper_feed(session=None, title="Test Paper", url="https://example.com/feed")
    assert len(posts) == 1
    assert posts[0].external_id == "good"


@pytest.mark.asyncio
async def test_entry_missing_link_with_default_permalink_guid_falls_back_to_guid_as_link():
    # Documents the feedparser quirk above rather than asserting a false expectation: when
    # <link> is absent and <guid> has no explicit isPermaLink (defaults to "true"), the entry
    # is NOT skipped - it just ends up with the guid string as a non-URL "link".
    xml = rss_feed(rss_item(guid="not-a-real-url", title="Permalink guid, no link", include_link=False))
    with _mock_feed_text(xml):
        posts = await fetch_newspaper_feed(session=None, title="Test Paper", url="https://example.com/feed")
    assert len(posts) == 1
    assert posts[0].url == "not-a-real-url"


@pytest.mark.asyncio
async def test_entry_missing_title_is_skipped_others_kept():
    xml = rss_feed(
        rss_item(guid="no-title", include_title=False, link="https://example.com/notitle")
        + rss_item(guid="good", title="Good entry", link="https://example.com/good")
    )
    with _mock_feed_text(xml):
        posts = await fetch_newspaper_feed(session=None, title="Test Paper", url="https://example.com/feed")
    assert len(posts) == 1
    assert posts[0].external_id == "good"


@pytest.mark.asyncio
async def test_entry_missing_both_link_and_title_is_skipped():
    xml = rss_feed(
        rss_item(guid="bare", include_title=False, include_link=False)
        + rss_item(guid="good", title="Good entry", link="https://example.com/good")
    )
    with _mock_feed_text(xml):
        posts = await fetch_newspaper_feed(session=None, title="Test Paper", url="https://example.com/feed")
    assert len(posts) == 1
    assert posts[0].external_id == "good"


@pytest.mark.asyncio
async def test_all_entries_missing_required_fields_returns_empty_list():
    xml = rss_feed(
        rss_item(include_link=False, guid_is_permalink=False) + rss_item(include_title=False)
    )
    with _mock_feed_text(xml):
        posts = await fetch_newspaper_feed(session=None, title="Test Paper", url="https://example.com/feed")
    assert posts == []


@pytest.mark.asyncio
async def test_html_tags_in_summary_are_stripped_via_clean_html():
    xml = rss_feed(
        rss_item(
            guid="html-body", title="Story with HTML body", link="https://example.com/html",
            description='<p>Breaking: <strong>markets</strong> rally.</p><br/><span>More at 11.</span>',
        )
    )
    with _mock_feed_text(xml):
        posts = await fetch_newspaper_feed(session=None, title="Test Paper", url="https://example.com/feed")
    assert len(posts) == 1
    assert posts[0].text == "Breaking: markets rally. More at 11."
    assert "<" not in posts[0].text and ">" not in posts[0].text


@pytest.mark.asyncio
async def test_entry_missing_guid_falls_back_to_link_as_external_id():
    xml = rss_feed(
        rss_item(include_guid=False, title="No guid", link="https://example.com/noguid")
    )
    with _mock_feed_text(xml):
        posts = await fetch_newspaper_feed(session=None, title="Test Paper", url="https://example.com/feed")
    assert len(posts) == 1
    assert posts[0].external_id == "https://example.com/noguid"


@pytest.mark.asyncio
async def test_entry_garbage_pub_date_falls_back_to_now_not_skipped():
    # feedparser can't turn "not-a-real-date" into published_parsed (it stays None), so this
    # falls back to "now" rather than dropping the post or raising.
    before = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
    xml = rss_feed(
        rss_item(guid="baddate", title="Bad date", link="https://example.com/baddate",
                  pub_date="not-a-real-date")
    )
    with _mock_feed_text(xml):
        posts = await fetch_newspaper_feed(session=None, title="Test Paper", url="https://example.com/feed")
    assert len(posts) == 1
    assert posts[0].published_at >= before


@pytest.mark.asyncio
async def test_entry_missing_pub_date_entirely_falls_back_to_now_not_skipped():
    from datetime import datetime, timezone
    before = datetime.now(timezone.utc)
    xml = rss_feed(
        rss_item(guid="nodate", title="No date", link="https://example.com/nodate", include_pub_date=False)
    )
    with _mock_feed_text(xml):
        posts = await fetch_newspaper_feed(session=None, title="Test Paper", url="https://example.com/feed")
    assert len(posts) == 1
    assert posts[0].published_at >= before
    assert posts[0].published_at.tzinfo is not None


@pytest.mark.asyncio
async def test_posts_are_truncated_to_posts_per_fetch(monkeypatch):
    monkeypatch.setattr("app.parsers.newspapers.POSTS_PER_FETCH", 2)
    xml = rss_feed(
        "".join(rss_item(guid=f"g{i}", title=f"Story {i}", link=f"https://example.com/{i}") for i in range(5))
    )
    with _mock_feed_text(xml):
        posts = await fetch_newspaper_feed(session=None, title="Test Paper", url="https://example.com/feed")
    assert len(posts) == 2


@pytest.mark.asyncio
async def test_parse_feed_raising_exception_returns_empty_list_not_raise():
    # Defensive/adversarial: if feedparser itself ever raises inside the worker thread
    # (rather than setting bozo), fetch_newspaper_feed must still not propagate it.
    with _mock_feed_text("<rss></rss>"), \
         patch("app.parsers.newspapers._parse_feed", side_effect=RuntimeError("boom: simulated parser crash")):
        posts = await fetch_newspaper_feed(session=None, title="Test Paper", url="https://example.com/feed")
    assert posts == []


# --------------------- fetch_newspaper_feed (via hand-built fake feed) ---------------------
# These bypass feedparser entirely (patching _parse_feed) to independently exercise fallback
# branches that feedparser's own RSS 2.0 field aliasing makes impossible to trigger via real XML.

class _FakeFeed:
    def __init__(self, entries, bozo=False, bozo_exception=None):
        self.bozo = bozo
        self.entries = entries
        self._bozo_exception = bozo_exception

    def get(self, key, default=None):
        if key == "bozo_exception":
            return self._bozo_exception
        return default


def _mock_parsed_feed(fake_feed):
    return patch("app.parsers.newspapers._parse_feed", return_value=fake_feed)


@pytest.mark.asyncio
async def test_summary_falls_back_to_description_when_summary_key_absent():
    entry = {
        "id": "g1", "link": "https://example.com/1", "title": "Story",
        "description": "<p>From description field</p>",
    }
    with _mock_feed_text("<rss></rss>"), _mock_parsed_feed(_FakeFeed([entry])):
        posts = await fetch_newspaper_feed(session=None, title="Test Paper", url="https://example.com/feed")
    assert len(posts) == 1
    assert posts[0].text == "From description field"


@pytest.mark.asyncio
async def test_summary_and_description_both_absent_gives_empty_text():
    entry = {"id": "g1", "link": "https://example.com/1", "title": "Story"}
    with _mock_feed_text("<rss></rss>"), _mock_parsed_feed(_FakeFeed([entry])):
        posts = await fetch_newspaper_feed(session=None, title="Test Paper", url="https://example.com/feed")
    assert len(posts) == 1
    assert posts[0].text == ""


@pytest.mark.asyncio
async def test_published_parsed_falls_back_to_updated_parsed_when_absent():
    entry = {
        "id": "g1", "link": "https://example.com/1", "title": "Story",
        "updated_parsed": (2022, 6, 15, 10, 0, 0, 0, 0, 0),
    }
    with _mock_feed_text("<rss></rss>"), _mock_parsed_feed(_FakeFeed([entry])):
        posts = await fetch_newspaper_feed(session=None, title="Test Paper", url="https://example.com/feed")
    assert len(posts) == 1
    assert posts[0].published_at.year == 2022
    assert posts[0].published_at.month == 6
    assert posts[0].published_at.day == 15


@pytest.mark.asyncio
async def test_both_published_and_updated_parsed_absent_falls_back_to_now():
    from datetime import datetime, timezone
    before = datetime.now(timezone.utc)
    entry = {"id": "g1", "link": "https://example.com/1", "title": "Story"}
    with _mock_feed_text("<rss></rss>"), _mock_parsed_feed(_FakeFeed([entry])):
        posts = await fetch_newspaper_feed(session=None, title="Test Paper", url="https://example.com/feed")
    assert len(posts) == 1
    assert posts[0].published_at >= before


@pytest.mark.asyncio
async def test_malformed_published_parsed_tuple_too_short_falls_back_to_now_not_raise():
    # A truncated struct_time-like tuple makes datetime(*parsed_time[:6]) raise TypeError
    # (missing required positional args) - must be swallowed, not propagated.
    from datetime import datetime, timezone
    before = datetime.now(timezone.utc)
    entry = {"id": "g1", "link": "https://example.com/1", "title": "Story", "published_parsed": (2024,)}
    with _mock_feed_text("<rss></rss>"), _mock_parsed_feed(_FakeFeed([entry])):
        posts = await fetch_newspaper_feed(session=None, title="Test Paper", url="https://example.com/feed")
    assert len(posts) == 1
    assert posts[0].published_at >= before


@pytest.mark.asyncio
async def test_malformed_published_parsed_out_of_range_values_falls_back_to_now_not_raise():
    # month=13 makes datetime(*parsed_time[:6]) raise ValueError - must be swallowed too.
    from datetime import datetime, timezone
    before = datetime.now(timezone.utc)
    entry = {
        "id": "g1", "link": "https://example.com/1", "title": "Story",
        "published_parsed": (2024, 13, 40, 99, 99, 99, 0, 0, 0),
    }
    with _mock_feed_text("<rss></rss>"), _mock_parsed_feed(_FakeFeed([entry])):
        posts = await fetch_newspaper_feed(session=None, title="Test Paper", url="https://example.com/feed")
    assert len(posts) == 1
    assert posts[0].published_at >= before


@pytest.mark.asyncio
async def test_id_falls_back_to_link_when_id_key_absent_via_fake_feed():
    entry = {"link": "https://example.com/idless", "title": "No id key at all"}
    with _mock_feed_text("<rss></rss>"), _mock_parsed_feed(_FakeFeed([entry])):
        posts = await fetch_newspaper_feed(session=None, title="Test Paper", url="https://example.com/feed")
    assert len(posts) == 1
    assert posts[0].external_id == "https://example.com/idless"


@pytest.mark.asyncio
async def test_bozo_feed_with_recovered_entries_is_not_dropped():
    # bozo=True (some parse warning/error) but entries were still recovered - the "and not
    # feed.entries" guard means this must NOT hit the early-return; the recovered entry
    # should still be processed normally.
    entry = {"id": "g1", "link": "https://example.com/1", "title": "Recovered despite bozo"}
    with _mock_feed_text("<rss></rss>"), \
         _mock_parsed_feed(_FakeFeed([entry], bozo=True, bozo_exception="some recoverable parse warning")):
        posts = await fetch_newspaper_feed(session=None, title="Test Paper", url="https://example.com/feed")
    assert len(posts) == 1
    assert posts[0].title == "Recovered despite bozo"


@pytest.mark.asyncio
async def test_bozo_feed_with_no_entries_returns_empty_list_gracefully():
    with _mock_feed_text("<rss></rss>"), \
         _mock_parsed_feed(_FakeFeed([], bozo=True, bozo_exception="totally broken")):
        posts = await fetch_newspaper_feed(session=None, title="Test Paper", url="https://example.com/feed")
    assert posts == []
