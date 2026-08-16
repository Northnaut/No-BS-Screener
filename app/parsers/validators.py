import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=15)
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_REDDIT_RETRY_ATTEMPTS = 2
_REDDIT_RETRY_DELAY_SECONDS = 2

_REDDIT_PATTERNS = [
    re.compile(r"^r/([A-Za-z0-9_]+)/?$", re.IGNORECASE),
    re.compile(r"^/r/([A-Za-z0-9_]+)/?$", re.IGNORECASE),
    re.compile(r"^(?:https?://)?(?:www\.)?reddit\.com/r/([A-Za-z0-9_]+)/?.*$", re.IGNORECASE),
]

_YOUTUBE_CHANNEL_ID_PATTERN = re.compile(
    r"^(?:https?://)?(?:www\.)?youtube\.com/channel/(UC[A-Za-z0-9_-]{22})/?.*$", re.IGNORECASE
)
_YOUTUBE_HANDLE_PATTERN = re.compile(
    r"^(?:https?://)?(?:www\.)?youtube\.com/@([A-Za-z0-9_.-]+)/?.*$", re.IGNORECASE
)
_YOUTUBE_CANONICAL_CHANNEL_ID = re.compile(
    r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[A-Za-z0-9_-]{22})">'
)
_YOUTUBE_CHANNEL_ID_IN_HTML = re.compile(r'"channelId":"(UC[A-Za-z0-9_-]{22})"')


@dataclass
class ValidatedSource:
    platform: str
    external_id: str
    title: Optional[str]
    url: str


def _extract_reddit_subreddit(raw: str) -> Optional[str]:
    raw = raw.strip()
    for pattern in _REDDIT_PATTERNS:
        match = pattern.match(raw)
        if match:
            return match.group(1)
    return None


def _extract_youtube_channel_id(raw: str) -> Optional[str]:
    raw = raw.strip()
    match = _YOUTUBE_CHANNEL_ID_PATTERN.match(raw)
    return match.group(1) if match else None


def extract_external_id(platform: str, raw_link: str) -> Optional[str]:
    if platform == "reddit":
        return _extract_reddit_subreddit(raw_link)
    if platform == "youtube":
        return _extract_youtube_channel_id(raw_link)
    return None


_REDDIT_DOMAINS = ["www.reddit.com", "old.reddit.com"]


async def fetch_reddit_json(session: aiohttp.ClientSession, subreddit: str, path: str = "new.json?limit=1") -> Optional[dict]:
    headers = {
        "User-Agent": _USER_AGENT,
        "Accept": "application/json",
    }

    for attempt in range(_REDDIT_RETRY_ATTEMPTS):
        for domain in _REDDIT_DOMAINS:
            url = f"https://{domain}/r/{subreddit}/{path}"
            try:
                async with session.get(url, headers=headers, timeout=_REQUEST_TIMEOUT) as resp:
                    if resp.status != 200:
                        logger.warning("Reddit request failed for r/%s via %s: HTTP %s", subreddit, domain, resp.status)
                        continue
                    content_type = resp.headers.get("Content-Type", "")
                    if "application/json" not in content_type:
                        logger.warning(
                            "Reddit request for r/%s via %s returned non-JSON content-type %s (likely blocked/redirected)",
                            subreddit, domain, content_type,
                        )
                        continue
                    return await resp.json()
            except Exception:
                logger.exception("Error fetching Reddit data for r/%s via %s", subreddit, domain)
                continue

        if attempt < _REDDIT_RETRY_ATTEMPTS - 1:
            await asyncio.sleep(_REDDIT_RETRY_DELAY_SECONDS)

    return None


async def _reddit_subreddit_exists(session: aiohttp.ClientSession, subreddit: str) -> bool:
    data = await fetch_reddit_json(session, subreddit)
    if data is None:
        return False
    return "data" in data and "children" in data.get("data", {})


async def _resolve_youtube_channel_id(session: aiohttp.ClientSession, handle: str) -> Optional[str]:
    url = f"https://www.youtube.com/@{handle}"
    try:
        async with session.get(url, headers={"User-Agent": _USER_AGENT}, timeout=_REQUEST_TIMEOUT) as resp:
            if resp.status != 200:
                logger.warning("YouTube handle resolution failed for @%s: HTTP %s", handle, resp.status)
                return None
            html = await resp.text()
            match = _YOUTUBE_CANONICAL_CHANNEL_ID.search(html)
            if match:
                return match.group(1)
            match = _YOUTUBE_CHANNEL_ID_IN_HTML.search(html)
            return match.group(1) if match else None
    except Exception:
        logger.exception("Error resolving YouTube handle @%s", handle)
        return None


async def _youtube_channel_title(session: aiohttp.ClientSession, channel_id: str) -> Optional[str]:
    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    try:
        async with session.get(url, headers={"User-Agent": _USER_AGENT}, timeout=_REQUEST_TIMEOUT) as resp:
            if resp.status != 200:
                return None
            xml = await resp.text()
            match = re.search(r"<title>(.*?)</title>", xml)
            return match.group(1) if match else None
    except Exception:
        logger.exception("Error fetching YouTube channel title for %s", channel_id)
        return None


async def validate_reddit_link(raw_link: str) -> Optional[ValidatedSource]:
    subreddit = _extract_reddit_subreddit(raw_link)
    if not subreddit:
        return None

    async with aiohttp.ClientSession() as session:
        if not await _reddit_subreddit_exists(session, subreddit):
            return None

    return ValidatedSource(
        platform="reddit",
        external_id=subreddit,
        title=f"r/{subreddit}",
        url=f"https://www.reddit.com/r/{subreddit}/",
    )


async def validate_youtube_link(raw_link: str) -> Optional[ValidatedSource]:
    raw_link = raw_link.strip()

    channel_id: Optional[str] = None
    match = _YOUTUBE_CHANNEL_ID_PATTERN.match(raw_link)
    if match:
        channel_id = match.group(1)

    async with aiohttp.ClientSession() as session:
        if not channel_id:
            handle_match = _YOUTUBE_HANDLE_PATTERN.match(raw_link)
            if not handle_match:
                return None
            handle = handle_match.group(1)
            channel_id = await _resolve_youtube_channel_id(session, handle)
            if not channel_id:
                return None

        title = await _youtube_channel_title(session, channel_id)
        if title is None:
            logger.warning("YouTube channel %s did not resolve to a valid RSS feed", channel_id)
            return None

    return ValidatedSource(
        platform="youtube",
        external_id=channel_id,
        title=title,
        url=f"https://www.youtube.com/channel/{channel_id}",
    )
