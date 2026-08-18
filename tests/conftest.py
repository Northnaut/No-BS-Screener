"""Shared test helpers for building synthetic Reddit-style Atom XML, RSS 2.0 XML (as
used by the newspaper feeds), and a minimal fake aiohttp.ClientSession stand-in, so
RSS/JSON fetch logic can be exercised without any real network calls (per
feedparser.parse() on a raw XML string, exactly like the production code path)."""
import html as htmllib

import pytest

from app.parsers import validators


@pytest.fixture(autouse=True)
def reset_reddit_throttle(monkeypatch):
    """The reddit request throttle is process-global state (the rate limit is per-IP, so it
    has to be). Left alone it makes the suite block on real multi-second sleeps whenever a
    test issues more than one request, and an adapted interval leaks between tests. Zero the
    interval and clear the timestamp so tests stay isolated and fast — the throttle's own
    behaviour is covered explicitly in test_reddit_rate_limit.py with sleep patched."""
    monkeypatch.setattr(validators, "_REDDIT_MIN_REQUEST_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(validators, "_reddit_interval", 0.0)
    monkeypatch.setattr(validators, "_reddit_last_request_time", 0.0)
    yield


def atom_entry(
    entry_id="t3_default",
    title="Default title",
    link="https://old.reddit.com/r/test/comments/default/",
    published="2024-01-01T12:00:00+00:00",
    content_html=None,
    include_id=True,
    include_title=True,
    include_link=True,
    include_published=True,
):
    """Builds a single <entry>...</entry> Atom fragment. Any of the four core fields can be
    omitted via include_*=False to simulate a malformed/partial Reddit RSS entry. content_html,
    when given, is HTML-escaped before embedding (matching how Reddit actually serves body HTML
    inside <content type="html">, i.e. as escaped text, not as literal nested XML nodes)."""
    parts = ["<entry>"]
    if include_id:
        parts.append(f"<id>{entry_id}</id>")
    if include_title:
        parts.append(f"<title>{title}</title>")
    if include_link:
        parts.append(f'<link href="{link}" />')
    if include_published:
        parts.append(f"<published>{published}</published>")
    if content_html is not None:
        parts.append(f'<content type="html">{htmllib.escape(content_html)}</content>')
    parts.append("</entry>")
    return "\n".join(parts)


def atom_feed(entries_xml: str = "") -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
<title>newest submissions : test</title>
{entries_xml}
</feed>"""


def rss_item(
    guid="guid-default",
    title="Default title",
    link="https://example.com/default",
    pub_date="Wed, 02 Oct 2024 15:00:00 +0000",
    description="Default description",
    include_guid=True,
    include_title=True,
    include_link=True,
    include_pub_date=True,
    include_description=True,
    guid_is_permalink=None,
):
    """Builds a single <item>...</item> RSS 2.0 fragment, as served by the real newspaper
    feeds (WSJ, NYT, BBC, etc. all use RSS 2.0, not Atom). Any field can be omitted via
    include_*=False to simulate a malformed/partial newspaper feed entry. title/description
    are HTML-escaped before embedding, matching how real feeds serve entities/markup inside
    <description> (e.g. "&lt;p&gt;...&lt;/p&gt;"), not as literal nested XML nodes.

    guid_is_permalink, when set to True/False, adds an explicit isPermaLink="true"/"false"
    attribute to <guid>. Per the RSS 2.0 spec, isPermaLink defaults to "true" when omitted -
    feedparser then backfills entry.link from the guid when <link> itself is absent (the RSS
    equivalent of Atom's guidislink quirk), so include_link=False alone does NOT guarantee a
    missing link unless isPermaLink="false" is set or <guid> is omitted entirely."""
    parts = ["<item>"]
    if include_title:
        parts.append(f"<title>{htmllib.escape(title)}</title>")
    if include_link:
        parts.append(f"<link>{link}</link>")
    if include_guid:
        if guid_is_permalink is None:
            parts.append(f"<guid>{guid}</guid>")
        else:
            attr = "true" if guid_is_permalink else "false"
            parts.append(f'<guid isPermaLink="{attr}">{guid}</guid>')
    if include_pub_date:
        parts.append(f"<pubDate>{pub_date}</pubDate>")
    if include_description:
        parts.append(f"<description>{htmllib.escape(description)}</description>")
    parts.append("</item>")
    return "\n".join(parts)


def rss_feed(items_xml: str = "") -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<title>Test Newspaper Feed</title>
<link>https://example.com</link>
<description>A test feed</description>
{items_xml}
</channel></rss>"""


class FakeResponse:
    """Stand-in for aiohttp.ClientResponse, usable as `async with session.get(...) as resp`."""

    def __init__(self, status=200, text="", json_data=None, content_type="application/atom+xml"):
        self.status = status
        self._text = text
        self._json_data = json_data
        self.headers = {"Content-Type": content_type}

    async def text(self):
        return self._text

    async def json(self):
        return self._json_data


class _FakeGetContext:
    def __init__(self, item):
        self._item = item

    async def __aenter__(self):
        if isinstance(self._item, Exception):
            raise self._item
        return self._item

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeSession:
    """Minimal aiohttp.ClientSession stand-in. `responses` is a list consumed in order, one
    per session.get() call. Each queued item is either a FakeResponse, or an Exception instance
    to simulate a network-level failure (raised inside the `async with` block, like a real
    aiohttp.ClientError would be)."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.requested_urls = []

    def get(self, url, headers=None, timeout=None):
        self.requested_urls.append(url)
        if not self._responses:
            raise AssertionError(f"FakeSession ran out of queued responses (requested {url})")
        item = self._responses.pop(0)
        return _FakeGetContext(item)
