import asyncio
import logging
from datetime import datetime, timezone

import feedparser

from app.parsers.base import FetchedPost

logger = logging.getLogger(__name__)

_YOUTUBE_FEED_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"


def _parse_feed(url: str):
    return feedparser.parse(url)


async def fetch_youtube_videos(channel_id: str) -> list[FetchedPost]:
    url = _YOUTUBE_FEED_URL.format(channel_id=channel_id)

    try:
        feed = await asyncio.to_thread(_parse_feed, url)
    except Exception as exc:
        raise RuntimeError(f"Failed to fetch YouTube feed for channel {channel_id}") from exc

    if feed.bozo and not feed.entries:
        raise RuntimeError(
            f"YouTube feed for channel {channel_id} could not be parsed: {feed.get('bozo_exception')}"
        )

    posts: list[FetchedPost] = []

    for entry in feed.entries:
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
