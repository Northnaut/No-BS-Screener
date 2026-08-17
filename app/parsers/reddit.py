import asyncio
import html
import logging
import re
from datetime import datetime, timezone
from typing import Optional

import aiohttp
import feedparser

from app.parsers.base import FetchedPost
from app.parsers.validators import fetch_reddit_json, fetch_reddit_rss

logger = logging.getLogger(__name__)

_SC_OFF_MARKER = "<!-- SC_OFF -->"
_SC_ON_MARKER = "<!-- SC_ON -->"
_MD_DIV_OPEN = '<div class="md">'
_TAG_RE = re.compile(r"<[^>]+>")


def _extract_rss_body_text(content_html: str) -> str:
    """Reddit's RSS wraps a text post's body as <!-- SC_OFF --><div class="md">...</div><!-- SC_ON -->
    followed by "submitted by ... [link] [comments]" boilerplate. Link/image posts have neither
    marker, so this returns '' for them, matching JSON's empty selftext for the same post types.

    The body itself can contain nested <div> (spoiler tags, tables), so a regex like
    <div class="md">(.*?)</div> would stop at the first inner </div> and truncate real content.
    Slicing between the SC_OFF/SC_ON markers (which always bound exactly the md div, verbatim
    per Reddit's template) avoids needing to match balanced tags at all."""
    content_html = content_html or ""
    off_idx = content_html.find(_SC_OFF_MARKER)
    on_idx = content_html.find(_SC_ON_MARKER)
    if off_idx == -1 or on_idx == -1 or on_idx <= off_idx:
        return ""

    inner = content_html[off_idx + len(_SC_OFF_MARKER):on_idx]
    if inner.startswith(_MD_DIV_OPEN):
        inner = inner[len(_MD_DIV_OPEN):]
    inner = inner.rstrip()
    if inner.endswith("</div>"):
        inner = inner[: -len("</div>")]

    text = _TAG_RE.sub(" ", inner)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _parse_rss(xml_text: str):
    return feedparser.parse(xml_text)


_NON_WWW_REDDIT_DOMAIN_RE = re.compile(r"://(?:old|new|np|amp)\.reddit\.com/")


def _normalize_reddit_url(url: str) -> str:
    """RSS can be fetched from old.reddit.com (or another alternate domain if one is ever
    added); alerts should always show a plain www.reddit.com link regardless of which
    domain actually served the feed."""
    return _NON_WWW_REDDIT_DOMAIN_RE.sub("://www.reddit.com/", url)


async def _fetch_via_rss(session: aiohttp.ClientSession, subreddit: str, limit: int) -> Optional[list[FetchedPost]]:
    """Returns None on any RSS-side failure (fetch, parse, or unexpected error while reading
    entries) so the caller falls back to JSON. Never lets an exception escape — this is the
    primary fetch path and must not be able to take the whole bot process down with it."""
    try:
        xml_text = await fetch_reddit_rss(session, subreddit, path=f"new/.rss?limit={limit}")
        if xml_text is None:
            return None

        feed = await asyncio.to_thread(_parse_rss, xml_text)
        if feed.bozo and not feed.entries:
            logger.warning("RSS feed for r/%s could not be parsed: %s", subreddit, feed.get("bozo_exception"))
            return None

        posts: list[FetchedPost] = []

        for entry in feed.entries[:limit]:
            try:
                external_id = entry.id
                title = entry.title
                link = entry.link
                published = entry.published
            except AttributeError:
                logger.warning("Skipping malformed RSS entry in r/%s: missing required field", subreddit)
                continue

            try:
                published_at = datetime.fromisoformat(published)
            except (ValueError, TypeError):
                logger.warning("Skipping RSS entry %s with unparseable date %r", external_id, published)
                continue

            if published_at.tzinfo is None:
                published_at = published_at.replace(tzinfo=timezone.utc)

            content_html = ""
            if entry.get("content"):
                content_html = entry["content"][0].get("value", "")

            posts.append(
                FetchedPost(
                    external_id=external_id,
                    title=title,
                    text=_extract_rss_body_text(content_html),
                    url=_normalize_reddit_url(link),
                    published_at=published_at,
                )
            )

        return posts
    except Exception:
        logger.exception("Unexpected error processing RSS feed for r/%s, falling back to JSON", subreddit)
        return None


async def fetch_reddit_posts(session: aiohttp.ClientSession, subreddit: str, limit: int = 25) -> list[FetchedPost]:
    posts = await _fetch_via_rss(session, subreddit, limit)
    if posts is not None:
        return posts

    logger.warning("RSS fetch failed for r/%s, falling back to JSON", subreddit)
    data = await fetch_reddit_json(session, subreddit, path=f"new.json?limit={limit}")
    if data is None:
        raise RuntimeError(f"Failed to fetch posts for r/{subreddit} (RSS and JSON both failed)")

    try:
        children = data.get("data", {}).get("children", [])
        json_posts: list[FetchedPost] = []

        for child in children:
            post_data = child.get("data", {})
            try:
                external_id = post_data["name"]
                title = post_data["title"]
                permalink = post_data["permalink"]
                created_utc = post_data["created_utc"]
            except KeyError:
                logger.warning("Skipping malformed Reddit post in r/%s: missing required field", subreddit)
                continue

            json_posts.append(
                FetchedPost(
                    external_id=external_id,
                    title=title,
                    text=post_data.get("selftext", ""),
                    url=f"https://www.reddit.com{permalink}",
                    published_at=datetime.fromtimestamp(created_utc, tz=timezone.utc),
                )
            )

        return json_posts
    except Exception as exc:
        raise RuntimeError(f"Failed to parse JSON fallback response for r/{subreddit}") from exc
