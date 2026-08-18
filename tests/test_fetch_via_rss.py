"""Tests for app.parsers.reddit._fetch_via_rss. `fetch_reddit_rss` (the network call) is
mocked at the module level it's imported into; feedparser itself parses real synthetic XML
strings, exactly like the production code path (feedparser.parse() on raw text)."""
from unittest.mock import AsyncMock, patch

import pytest

from app.parsers.reddit import _fetch_via_rss
from tests.conftest import atom_entry, atom_feed


def _mock_rss(xml_text):
    return patch("app.parsers.reddit.fetch_reddit_rss", AsyncMock(return_value=xml_text))


@pytest.mark.asyncio
async def test_normal_feed_returns_parsed_posts():
    xml = atom_feed(
        atom_entry(entry_id="t3_a", title="First", link="https://old.reddit.com/r/test/comments/a/",
                   published="2024-01-01T12:00:00+00:00", content_html='<!-- SC_OFF --><div class="md"><p>Body A</p></div><!-- SC_ON -->')
        + atom_entry(entry_id="t3_b", title="Second", link="https://www.reddit.com/r/test/comments/b/",
                     published="2024-01-02T08:30:00+00:00")
    )
    with _mock_rss(xml):
        posts = await _fetch_via_rss(session=None, subreddit="test", limit=25)

    assert posts is not None
    assert len(posts) == 2
    assert posts[0].external_id == "t3_a"
    assert posts[0].title == "First"
    assert posts[0].text == "Body A"
    assert posts[0].url == "https://www.reddit.com/r/test/comments/a/"
    assert posts[0].published_at.year == 2024
    assert posts[1].external_id == "t3_b"
    assert posts[1].text == ""


@pytest.mark.asyncio
async def test_limit_truncates_entries():
    xml = atom_feed("".join(atom_entry(entry_id=f"t3_{i}") for i in range(5)))
    with _mock_rss(xml):
        posts = await _fetch_via_rss(session=None, subreddit="test", limit=2)

    assert posts is not None
    assert len(posts) == 2


@pytest.mark.asyncio
async def test_rss_fetch_failure_returns_none():
    with _mock_rss(None):
        posts = await _fetch_via_rss(session=None, subreddit="test", limit=25)
    assert posts is None


@pytest.mark.asyncio
async def test_zero_entries_feed_returns_empty_list_not_none():
    xml = atom_feed("")
    with _mock_rss(xml):
        posts = await _fetch_via_rss(session=None, subreddit="test", limit=25)
    assert posts == []


@pytest.mark.asyncio
async def test_completely_garbage_non_xml_returns_none():
    with _mock_rss("this is not xml at all <<<>>> {}"):
        posts = await _fetch_via_rss(session=None, subreddit="test", limit=25)
    assert posts is None


@pytest.mark.asyncio
async def test_html_challenge_page_leaking_through_returns_none_or_empty_without_raising():
    # fetch_reddit_rss itself guards against this (see validators content check), but this
    # confirms _fetch_via_rss is *also* defensive if a non-feed HTML string ever reaches it.
    html_page = "<html><head><title>Just a moment...</title></head><body>Checking your browser</body></html>"
    with _mock_rss(html_page):
        posts = await _fetch_via_rss(session=None, subreddit="test", limit=25)
    assert posts is None or posts == []


@pytest.mark.asyncio
async def test_truncated_mid_document_xml_does_not_raise():
    truncated = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<feed xmlns="http://www.w3.org/2005/Atom">\n<title>reddit</title>\n<entry>\n'
        '<id>t3_abc123</id>\n<title>Some title</title>\n'
        '<link href="https://old.reddit.com/r/test/comments/abc123/some_title/" />\n'
        '<published>2024-01-01T12:00:00+00:00</published>\n<content type="html">body'
    )
    with _mock_rss(truncated):
        posts = await _fetch_via_rss(session=None, subreddit="test", limit=25)
    # feedparser recovers a partial entry here; must not raise either way.
    assert posts is None or isinstance(posts, list)


@pytest.mark.asyncio
async def test_entry_missing_id_is_skipped_others_kept():
    xml = atom_feed(
        atom_entry(include_id=False, title="No id here")
        + atom_entry(entry_id="t3_good", title="Good entry")
    )
    with _mock_rss(xml):
        posts = await _fetch_via_rss(session=None, subreddit="test", limit=25)
    assert posts is not None
    assert len(posts) == 1
    assert posts[0].external_id == "t3_good"


@pytest.mark.asyncio
async def test_entry_missing_title_is_skipped_others_kept():
    xml = atom_feed(
        atom_entry(entry_id="t3_notitle", include_title=False)
        + atom_entry(entry_id="t3_good", title="Good entry")
    )
    with _mock_rss(xml):
        posts = await _fetch_via_rss(session=None, subreddit="test", limit=25)
    assert posts is not None
    assert [p.external_id for p in posts] == ["t3_good"]


@pytest.mark.asyncio
async def test_entry_missing_link_falls_back_to_atom_id_per_feedparser_guidislink():
    # Atom spec fallback: when <link> is absent but <id> is present, feedparser treats the id
    # as the link (guidislink=True) instead of raising AttributeError, so this entry is *not*
    # skipped - it just ends up with a non-URL string as its link. Real Reddit RSS always
    # includes <link>, so this is a documented feedparser quirk rather than a practical
    # concern; the important thing is it doesn't raise.
    xml = atom_feed(
        atom_entry(entry_id="t3_nolink", include_link=False)
        + atom_entry(entry_id="t3_good", title="Good entry")
    )
    with _mock_rss(xml):
        posts = await _fetch_via_rss(session=None, subreddit="test", limit=25)
    assert posts is not None
    assert [p.external_id for p in posts] == ["t3_nolink", "t3_good"]
    assert posts[0].url == "t3_nolink"  # not skipped, but also not a real URL - see comment above


@pytest.mark.asyncio
async def test_entry_missing_published_gets_fallback_timestamp_not_skipped():
    # published_at isn't used for dedup/display anywhere downstream, so a missing date isn't
    # worth losing the post over - it gets a "now" fallback instead of being dropped.
    xml = atom_feed(
        atom_entry(entry_id="t3_nopub", include_published=False)
        + atom_entry(entry_id="t3_good", title="Good entry")
    )
    with _mock_rss(xml):
        posts = await _fetch_via_rss(session=None, subreddit="test", limit=25)
    assert posts is not None
    assert [p.external_id for p in posts] == ["t3_nopub", "t3_good"]
    assert posts[0].published_at.tzinfo is not None


@pytest.mark.asyncio
async def test_all_entries_missing_required_fields_returns_empty_list():
    xml = atom_feed(atom_entry(include_id=False) + atom_entry(include_title=False))
    with _mock_rss(xml):
        posts = await _fetch_via_rss(session=None, subreddit="test", limit=25)
    assert posts == []


@pytest.mark.asyncio
async def test_garbage_published_date_gets_fallback_timestamp_not_skipped():
    # feedparser can't turn "not-a-real-date" into published_parsed, so this falls back to
    # "now" rather than dropping the post.
    xml = atom_feed(
        atom_entry(entry_id="t3_baddate", published="not-a-real-date")
        + atom_entry(entry_id="t3_good", published="2024-01-01T12:00:00+00:00")
    )
    with _mock_rss(xml):
        posts = await _fetch_via_rss(session=None, subreddit="test", limit=25)
    assert posts is not None
    assert [p.external_id for p in posts] == ["t3_baddate", "t3_good"]


@pytest.mark.asyncio
async def test_rfc822_published_date_is_parsed_correctly():
    # If Reddit ever served RSS 2.0 instead of Atom, <pubDate> comes through as a raw RFC822
    # string ("Wed, 02 Oct 2024 15:00:00 +0000"). feedparser normalizes this into
    # published_parsed regardless of dialect, so this is now parsed correctly instead of
    # silently dropping every entry until a code fix ships.
    rss2 = (
        '<?xml version="1.0" encoding="UTF-8"?>\n<rss version="2.0"><channel><title>reddit</title>\n'
        '<item><title>RSS2 item</title><link>https://old.reddit.com/r/test/comments/rss2item/</link>'
        '<guid>t3_rss2item</guid><pubDate>Wed, 02 Oct 2024 15:00:00 +0000</pubDate>'
        '<description>desc</description></item></channel></rss>'
    )
    with _mock_rss(rss2):
        posts = await _fetch_via_rss(session=None, subreddit="test", limit=25)
    assert posts is not None
    assert len(posts) == 1
    assert posts[0].external_id == "t3_rss2item"
    assert posts[0].published_at.year == 2024
    assert posts[0].published_at.month == 10
    assert posts[0].published_at.day == 2


@pytest.mark.asyncio
async def test_naive_published_date_without_tz_is_treated_as_utc():
    xml = atom_feed(atom_entry(entry_id="t3_naive", published="2024-01-01T12:00:00"))
    with _mock_rss(xml):
        posts = await _fetch_via_rss(session=None, subreddit="test", limit=25)
    assert posts is not None
    assert len(posts) == 1
    assert posts[0].published_at.tzinfo is not None


@pytest.mark.asyncio
async def test_deeply_nested_body_markup_survives_end_to_end():
    body = (
        '<!-- SC_OFF --><div class="md">'
        '<div class="spoiler"><div><table><tr><td>deep <div>nested</div> content</td></tr></table></div></div>'
        '<p>end of body</p></div><!-- SC_ON -->'
    )
    xml = atom_feed(atom_entry(entry_id="t3_nested", content_html=body))
    with _mock_rss(xml):
        posts = await _fetch_via_rss(session=None, subreddit="test", limit=25)
    assert posts is not None
    assert len(posts) == 1
    assert "deep" in posts[0].text
    assert "nested" in posts[0].text
    assert "end of body" in posts[0].text


@pytest.mark.asyncio
async def test_extremely_large_body_does_not_raise_or_truncate():
    big = "word " * 200_000
    body = f'<!-- SC_OFF --><div class="md"><p>{big}</p></div><!-- SC_ON -->'
    xml = atom_feed(atom_entry(entry_id="t3_big", content_html=body))
    with _mock_rss(xml):
        posts = await _fetch_via_rss(session=None, subreddit="test", limit=25)
    assert posts is not None
    assert len(posts) == 1
    assert posts[0].text.count("word") == 200_000


@pytest.mark.asyncio
async def test_illegal_xml_control_characters_do_not_raise():
    xml = (
        '<?xml version="1.0"?>\n<feed xmlns="http://www.w3.org/2005/Atom">\n<entry>\n'
        '<id>t3_ctrl</id>\n<title>Bad \x01\x02 chars</title>\n'
        '<link href="https://old.reddit.com/r/test/comments/ctrl/" />\n'
        '<published>2024-01-01T12:00:00+00:00</published>\n</entry>\n</feed>'
    )
    with _mock_rss(xml):
        posts = await _fetch_via_rss(session=None, subreddit="test", limit=25)
    assert posts is None or isinstance(posts, list)


@pytest.mark.asyncio
async def test_unexpected_exception_during_entry_processing_is_swallowed():
    """Defensive/adversarial case: an entry object that raises something other than
    AttributeError while being read (e.g. a corrupt feedparser dict implementation, or any
    future code change that reads more fields than id/title/link/published without updating
    the narrow except clause). The outer try/except in _fetch_via_rss must still catch this
    and return None rather than let it escape."""

    class ExplodingEntry(dict):
        id = "t3_explode"
        title = "Explode"
        link = "https://old.reddit.com/r/test/comments/explode/"
        published = "2024-01-01T12:00:00+00:00"

        def get(self, key, default=None):
            if key == "content":
                raise RuntimeError("boom: simulated unexpected failure reading content")
            return super().get(key, default)

    class FakeFeed:
        bozo = False
        entries = [ExplodingEntry()]

        def get(self, key, default=None):
            return default

    with patch("app.parsers.reddit.fetch_reddit_rss", AsyncMock(return_value="<feed></feed>")), \
         patch("app.parsers.reddit._parse_rss", return_value=FakeFeed()):
        posts = await _fetch_via_rss(session=None, subreddit="test", limit=25)

    assert posts is None
