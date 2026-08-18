import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Optional

import aiohttp
from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.tl.types import Channel as TelethonChannel

from app.services import userbot

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=15)
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_REDDIT_RETRY_ATTEMPTS = 2
_REDDIT_RETRY_DELAY_SECONDS = 2
_REDDIT_RATE_LIMIT_BACKOFF_SECONDS = 10
_REDDIT_RATE_LIMIT_MAX_BACKOFF_SECONDS = 60

# Reddit blocks browser-spoofed User-Agents from non-residential IPs — that fingerprint is
# exactly what their abuse system targets, and it was producing 8.5k 429s + 6k 403s a day
# against a 9.8% success rate. Their API terms ask for a unique descriptive UA instead.
_REDDIT_USER_AGENT = "NewsScreener/1.0 (personal news-alert bot; contact via repo owner)"

# Base gap between any two outgoing reddit.com requests, adapted at runtime: a 429 doubles
# it, sustained success decays it back toward the floor. A flat interval can't respond to
# Reddit tightening or loosening, which is why the previous fixed 20s neither prevented the
# rate limiting nor recovered from it.
_REDDIT_MIN_REQUEST_INTERVAL_SECONDS = 6.0
_REDDIT_MAX_REQUEST_INTERVAL_SECONDS = 60.0

_reddit_rate_limit_lock = asyncio.Lock()
_reddit_last_request_time = 0.0
_reddit_interval = _REDDIT_MIN_REQUEST_INTERVAL_SECONDS


async def _throttle_reddit_request() -> None:
    """Reddit's anonymous rate limit is per-IP, not per-subreddit. The polling cycle fetches
    every subscribed subreddit back-to-back with no gap between them, so even though each
    subreddit is only hit once per cycle, a user with a dozen+ reddit sources trips the limit
    within seconds. Serializing every outgoing reddit.com request (RSS, JSON, validation)
    behind a shared minimum gap keeps the whole process under the limit regardless of how many
    subreddits or callers are involved."""
    global _reddit_last_request_time
    async with _reddit_rate_limit_lock:
        loop = asyncio.get_running_loop()
        now = loop.time()
        wait = _reddit_last_request_time + _reddit_interval - now
        if wait > 0:
            await asyncio.sleep(wait)
        _reddit_last_request_time = loop.time()


def _note_reddit_rate_limited() -> None:
    """Back the whole process off after a 429. Multiplicative increase because the limit is
    per-IP: every caller shares the same budget, so a single slow-down has to apply globally."""
    global _reddit_interval
    previous = _reddit_interval
    _reddit_interval = min(_reddit_interval * 2, _REDDIT_MAX_REQUEST_INTERVAL_SECONDS)
    if _reddit_interval != previous:
        logger.warning(
            "Reddit rate limit hit, widening request interval %.1fs -> %.1fs", previous, _reddit_interval
        )


def _note_reddit_success() -> None:
    """Decay back toward the floor so a single bad patch doesn't permanently throttle polling."""
    global _reddit_interval
    if _reddit_interval > _REDDIT_MIN_REQUEST_INTERVAL_SECONDS:
        _reddit_interval = max(_REDDIT_MIN_REQUEST_INTERVAL_SECONDS, _reddit_interval * 0.9)

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

_TELEGRAM_PATTERNS = [
    re.compile(r"^@([A-Za-z][A-Za-z0-9_]{4,31})$", re.IGNORECASE),
    re.compile(r"^(?:https?://)?t\.me/([A-Za-z][A-Za-z0-9_]{4,31})/?$", re.IGNORECASE),
]


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


def _extract_telegram_channel(raw: str) -> Optional[str]:
    raw = raw.strip()
    for pattern in _TELEGRAM_PATTERNS:
        match = pattern.match(raw)
        if match:
            return match.group(1)
    return None


_REDDIT_DOMAINS = ["www.reddit.com", "old.reddit.com"]


def _parse_retry_after(raw: Optional[str]) -> float:
    if raw:
        try:
            return min(max(float(raw), 1.0), _REDDIT_RATE_LIMIT_MAX_BACKOFF_SECONDS)
        except ValueError:
            pass
    return _REDDIT_RATE_LIMIT_BACKOFF_SECONDS


async def fetch_reddit_json(session: aiohttp.ClientSession, subreddit: str, path: str = "new.json?limit=1") -> Optional[dict]:
    headers = {
        "User-Agent": _REDDIT_USER_AGENT,
        "Accept": "application/json",
    }

    for attempt in range(_REDDIT_RETRY_ATTEMPTS):
        for domain in _REDDIT_DOMAINS:
            url = f"https://{domain}/r/{subreddit}/{path}"
            await _throttle_reddit_request()
            try:
                async with session.get(url, headers=headers, timeout=_REQUEST_TIMEOUT) as resp:
                    if resp.status == 429:
                        _note_reddit_rate_limited()
                        retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
                        logger.warning(
                            "Reddit rate-limited request for r/%s via %s, backing off for %ss",
                            subreddit, domain, retry_after,
                        )
                        await asyncio.sleep(retry_after)
                        continue
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
                    _note_reddit_success()
                    return await resp.json()
            except Exception:
                logger.exception("Error fetching Reddit data for r/%s via %s", subreddit, domain)
                continue

        if attempt < _REDDIT_RETRY_ATTEMPTS - 1:
            await asyncio.sleep(_REDDIT_RETRY_DELAY_SECONDS)

    return None


async def fetch_reddit_rss(session: aiohttp.ClientSession, subreddit: str, path: str = "new/.rss?limit=1") -> Optional[str]:
    headers = {
        "User-Agent": _REDDIT_USER_AGENT,
        "Accept": "application/atom+xml, application/rss+xml, application/xml, text/xml",
    }

    for attempt in range(_REDDIT_RETRY_ATTEMPTS):
        for domain in _REDDIT_DOMAINS:
            url = f"https://{domain}/r/{subreddit}/{path}"
            await _throttle_reddit_request()
            try:
                async with session.get(url, headers=headers, timeout=_REQUEST_TIMEOUT) as resp:
                    if resp.status == 429:
                        _note_reddit_rate_limited()
                        retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
                        logger.warning(
                            "Reddit RSS rate-limited request for r/%s via %s, backing off for %ss",
                            subreddit, domain, retry_after,
                        )
                        await asyncio.sleep(retry_after)
                        continue
                    if resp.status != 200:
                        logger.warning("Reddit RSS request failed for r/%s via %s: HTTP %s", subreddit, domain, resp.status)
                        continue
                    text = await resp.text()
                    if "<feed" not in text and "<rss" not in text:
                        logger.warning(
                            "Reddit RSS request for r/%s via %s returned a non-feed response (likely blocked/redirected)",
                            subreddit, domain,
                        )
                        continue
                    _note_reddit_success()
                    return text
            except Exception:
                logger.exception("Error fetching Reddit RSS for r/%s via %s", subreddit, domain)
                continue

        if attempt < _REDDIT_RETRY_ATTEMPTS - 1:
            await asyncio.sleep(_REDDIT_RETRY_DELAY_SECONDS)

    return None


async def _reddit_subreddit_exists(session: aiohttp.ClientSession, subreddit: str) -> bool:
    rss_text = await fetch_reddit_rss(session, subreddit)
    if rss_text is not None:
        return True

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


async def validate_telegram_link(client: Optional[TelegramClient], raw_link: str) -> Optional[ValidatedSource]:
    username = _extract_telegram_channel(raw_link)
    if not username or client is None:
        return None

    if userbot.flood_wait_remaining() > 0:
        logger.warning("Skipping Telegram validation for @%s: userbot is in a flood-wait cooldown", username)
        return None

    try:
        entity = await client.get_entity(username)
    except FloodWaitError as exc:
        userbot.register_flood_wait(exc.seconds)
        logger.warning(
            "Telegram flood wait while validating @%s: pausing all Telegram usage for %ds", username, exc.seconds
        )
        return None
    except Exception:
        logger.exception("Error resolving Telegram channel @%s", username)
        return None

    if not isinstance(entity, TelethonChannel):
        return None

    resolved_username = entity.username or username
    title = entity.title or f"@{resolved_username}"

    return ValidatedSource(
        platform="telegram",
        external_id=resolved_username,
        title=title,
        url=f"https://t.me/{resolved_username}",
    )
