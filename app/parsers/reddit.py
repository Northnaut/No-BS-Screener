import logging
from datetime import datetime, timezone

import aiohttp

from app.parsers.base import FetchedPost
from app.parsers.validators import fetch_reddit_json

logger = logging.getLogger(__name__)


async def fetch_reddit_posts(session: aiohttp.ClientSession, subreddit: str, limit: int = 25) -> list[FetchedPost]:
    data = await fetch_reddit_json(session, subreddit, path=f"new.json?limit={limit}")
    if data is None:
        raise RuntimeError(f"Failed to fetch posts for r/{subreddit} (all domains/attempts failed)")

    children = data.get("data", {}).get("children", [])
    posts: list[FetchedPost] = []

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

        posts.append(
            FetchedPost(
                external_id=external_id,
                title=title,
                text=post_data.get("selftext", ""),
                url=f"https://www.reddit.com{permalink}",
                published_at=datetime.fromtimestamp(created_utc, tz=timezone.utc),
            )
        )

    return posts
