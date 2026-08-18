import asyncio
import logging
from datetime import datetime, timezone

import aiohttp
import feedparser

from app.config import POSTS_PER_FETCH
from app.parsers.base import FetchedPost

logger = logging.getLogger(__name__)

_YOUTUBE_FEED_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_FEED_TIMEOUT = aiohttp.ClientTimeout(total=12)


def _parse_feed(xml_text: str):
    return feedparser.parse(xml_text)


async def fetch_youtube_videos(session: aiohttp.ClientSession, channel_id: str) -> list[FetchedPost]:
    """Fetches over the shared aiohttp session rather than letting feedparser do its own
    urllib request. feedparser.parse(url) fetches synchronously with NO timeout — a
    channel that accepts the connection and then never responds would hang the worker
    thread forever, and because the discovery loop is sequential and the job is registered
    max_instances=1, that silently kills every later cycle too."""
    url = _YOUTUBE_FEED_URL.format(channel_id=channel_id)

    try:
        async with session.get(url, headers={"User-Agent": _USER_AGENT}, timeout=_FEED_TIMEOUT) as resp:
            if resp.status != 200:
                raise RuntimeError(
                    f"YouTube feed for channel {channel_id} returned HTTP {resp.status}"
                )
            xml_text = await resp.text()
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"Failed to fetch YouTube feed for channel {channel_id}") from exc

    try:
        feed = await asyncio.to_thread(_parse_feed, xml_text)
    except Exception as exc:
        raise RuntimeError(f"Failed to parse YouTube feed for channel {channel_id}") from exc

    if feed.bozo and not feed.entries:
        raise RuntimeError(
            f"YouTube feed for channel {channel_id} could not be parsed: {feed.get('bozo_exception')}"
        )

    posts: list[FetchedPost] = []

    for entry in feed.entries[:POSTS_PER_FETCH]:
        try:
            external_id = entry.id
            title = entry.title
            link = entry.link
            published = entry.published
        except AttributeError:
            logger.warning("Skipping malformed YouTube entry for channel %s: missing required field", channel_id)
            continue

        try:
            published_at = datetime.fromisoformat(published)
        except ValueError:
            logger.warning("Skipping YouTube entry %s with unparseable date '%s'", external_id, published)
            continue

        if published_at.tzinfo is None:
            published_at = published_at.replace(tzinfo=timezone.utc)

        posts.append(
            FetchedPost(
                external_id=external_id,
                title=title,
                text=getattr(entry, "summary", ""),
                url=link,
                published_at=published_at,
            )
        )

    return posts
