import asyncio
import html as htmllib
import logging
import re
from datetime import datetime, timezone
from typing import Optional

import aiohttp
import feedparser

from app.config import POSTS_PER_FETCH
from app.database.queries import delete_stale_newspaper_sources, upsert_newspaper_source
from app.parsers.base import FetchedPost

logger = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# 6s was measured to be too tight for some legitimately-slow-but-healthy feeds (e.g.
# Washington Post consistently takes 8-10.5s to respond on this network's routing), which
# made those feeds fail deterministically on every attempt rather than just flakily. 12s
# gives comfortable headroom above the observed worst case without meaningfully delaying
# the rest of the batch, since a slow feed only holds up its own semaphore slot.
_FEED_TIMEOUT = aiohttp.ClientTimeout(total=12)
_FEED_RETRY_ATTEMPTS = 2
_FEED_RETRY_DELAY_SECONDS = 2
_TAG_RE = re.compile(r"<[^>]+>")

CATEGORY_ECONOMY = "economy"
CATEGORY_CRYPTO = "crypto"
CATEGORY_POLITICS = "politics"
CATEGORY_TECH = "tech"

CATEGORY_LABELS = {
    CATEGORY_ECONOMY: "💰 Economy & Markets",
    CATEGORY_CRYPTO: "🪙 Crypto & Web3",
    CATEGORY_POLITICS: "🌍 World & Politics",
    CATEGORY_TECH: "🤖 Tech & AI",
}

# (title, category, RSS feed URL, homepage URL). This is the single source of truth for
# which newspapers exist — re-synced into the `sources` table on every startup via
# seed_newspaper_sources(). Each title must be unique (it doubles as the source's
# external_id since these feeds aren't user-added, unlike Reddit/YouTube/Telegram). The
# homepage URL is only used for the "Sources" reference list in the bot UI — it's never
# fetched, so it doesn't need to be the RSS endpoint, just where the outlet actually lives.
NEWSPAPER_FEEDS: list[tuple[str, str, str, str]] = [
    # ------------------ ECONOMY, BUSINESS & MARKETS ------------------
    ("WSJ Markets", CATEGORY_ECONOMY, "https://feeds.a.dj.com/rss/RSSMarketsMain.xml", "https://www.wsj.com/market-data"),
    ("WSJ Business", CATEGORY_ECONOMY, "https://feeds.a.dj.com/rss/WSJcomUSBusiness.xml", "https://www.wsj.com/business"),
    ("CNBC Top News", CATEGORY_ECONOMY, "https://search.cnbc.com/rs/search/combinedList/view.xml?partnerId=wrss01&id=100003114", "https://www.cnbc.com"),
    ("CNBC Finance", CATEGORY_ECONOMY, "https://search.cnbc.com/rs/search/combinedList/view.xml?partnerId=wrss01&id=10000664", "https://www.cnbc.com/finance/"),
    ("MarketWatch Top Stories", CATEGORY_ECONOMY, "https://feeds.content.dowjones.io/public/rss/mw_topstories", "https://www.marketwatch.com"),
    ("MarketWatch Real Time", CATEGORY_ECONOMY, "https://feeds.content.dowjones.io/public/rss/mw_realtimeheadlines", "https://www.marketwatch.com/latest-news"),
    ("Financial Times (World)", CATEGORY_ECONOMY, "https://www.ft.com/world?format=rss", "https://www.ft.com/world"),
    ("The Economist (Finance)", CATEGORY_ECONOMY, "https://www.economist.com/finance-and-economics/rss.xml", "https://www.economist.com/finance-and-economics"),
    ("Fortune", CATEGORY_ECONOMY, "https://fortune.com/feed", "https://fortune.com"),
    ("Forbes Business", CATEGORY_ECONOMY, "https://www.forbes.com/business/feed/", "https://www.forbes.com/business/"),
    ("Yahoo Finance", CATEGORY_ECONOMY, "https://finance.yahoo.com/news/rssindex", "https://finance.yahoo.com"),
    ("Investing.com", CATEGORY_ECONOMY, "https://www.investing.com/rss/news.rss", "https://www.investing.com"),

    # ------------------ CRYPTO, BLOCKCHAIN & WEB3 ------------------
    ("CoinDesk", CATEGORY_CRYPTO, "https://www.coindesk.com/arc/outboundfeeds/rss/", "https://www.coindesk.com"),
    ("Cointelegraph", CATEGORY_CRYPTO, "https://cointelegraph.com/rss", "https://cointelegraph.com"),
    ("Decrypt", CATEGORY_CRYPTO, "https://decrypt.co/feed", "https://decrypt.co"),
    ("The Block", CATEGORY_CRYPTO, "https://www.theblock.co/rss.xml", "https://www.theblock.co"),
    ("Bitcoin Magazine", CATEGORY_CRYPTO, "https://bitcoinmagazine.com/.rss/full/", "https://bitcoinmagazine.com"),
    ("CryptoSlate", CATEGORY_CRYPTO, "https://cryptoslate.com/feed/", "https://cryptoslate.com"),

    # ------------------ WORLD POLITICS, SOCIETY & MEDIA ------------------
    ("NYT World News", CATEGORY_POLITICS, "https://rss.nytimes.com/services/xml/rss/nyt/World.xml", "https://www.nytimes.com/section/world"),
    ("NYT US News", CATEGORY_POLITICS, "https://rss.nytimes.com/services/xml/rss/nyt/US.xml", "https://www.nytimes.com/section/us"),
    ("Washington Post World", CATEGORY_POLITICS, "https://feeds.washingtonpost.com/rss/world", "https://www.washingtonpost.com/world/"),
    ("Washington Post Politics", CATEGORY_POLITICS, "https://feeds.washingtonpost.com/rss/politics", "https://www.washingtonpost.com/politics/"),
    ("BBC Top Stories", CATEGORY_POLITICS, "http://feeds.bbci.co.uk/news/rss.xml", "https://www.bbc.com/news"),
    ("BBC World News", CATEGORY_POLITICS, "http://feeds.bbci.co.uk/news/world/rss.xml", "https://www.bbc.com/news/world"),
    ("The Guardian (World)", CATEGORY_POLITICS, "https://www.theguardian.com/world/rss", "https://www.theguardian.com/world"),
    ("Politico", CATEGORY_POLITICS, "https://rss.politico.com/politics-news.xml", "https://www.politico.com"),
    ("Axios", CATEGORY_POLITICS, "https://api.axios.com/feed/", "https://www.axios.com"),
    ("NPR News", CATEGORY_POLITICS, "https://feeds.npr.org/1001/rss.xml", "https://www.npr.org/sections/news/"),
    ("Al Jazeera", CATEGORY_POLITICS, "https://www.aljazeera.com/xml/rss/all.xml", "https://www.aljazeera.com"),
    ("Deutsche Welle (DW)", CATEGORY_POLITICS, "https://rss.dw.com/xml/rss-en-all", "https://www.dw.com/en/top-stories/s-9097"),
    ("Time Magazine", CATEGORY_POLITICS, "https://time.com/feed/", "https://time.com"),

    # ------------------ TECHNOLOGY, AI & INNOVATION ------------------
    ("TechCrunch", CATEGORY_TECH, "https://techcrunch.com/feed/", "https://techcrunch.com"),
    ("The Verge", CATEGORY_TECH, "https://www.theverge.com/rss/index.xml", "https://www.theverge.com"),
    ("Wired", CATEGORY_TECH, "https://www.wired.com/feed/rss", "https://www.wired.com"),
    ("Ars Technica", CATEGORY_TECH, "https://feeds.arstechnica.com/arstechnica/index", "https://arstechnica.com"),
    ("VentureBeat", CATEGORY_TECH, "https://venturebeat.com/feed/", "https://venturebeat.com"),
    ("MIT Tech Review", CATEGORY_TECH, "https://www.technologyreview.com/feed/", "https://www.technologyreview.com"),
    ("Hacker News Best", CATEGORY_TECH, "https://news.ycombinator.com/rss", "https://news.ycombinator.com"),
]


async def seed_newspaper_sources() -> None:
    """Idempotently syncs NEWSPAPER_FEEDS into the sources table. Safe to call on every
    startup — existing sources keep their is_bootstrapped/fail_count state. Also removes
    any previously-seeded newspaper source no longer in NEWSPAPER_FEEDS (e.g. a feed that
    got dropped from the list), so the code list stays the single source of truth."""
    for title, category, url, _homepage in NEWSPAPER_FEEDS:
        try:
            await upsert_newspaper_source(title, title, url, category)
        except Exception:
            logger.exception("Failed to seed newspaper source '%s'", title)

    try:
        removed = await delete_stale_newspaper_sources([title for title, _, _, _ in NEWSPAPER_FEEDS])
        if removed:
            logger.info("Removed %d newspaper source(s) no longer in the feed list", removed)
    except Exception:
        logger.exception("Failed to remove stale newspaper sources")

    logger.info("Seeded %d newspaper source(s)", len(NEWSPAPER_FEEDS))


def _clean_html(value: str) -> str:
    text = _TAG_RE.sub(" ", value or "")
    # Feeds serve entity-escaped text, so without this the literal "&amp;" / "&#8217;" reach
    # both the AI prompt and the user-facing "original" style. The Reddit parser already
    # unescapes; this path did not.
    text = htmllib.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _parse_feed(xml_text: str):
    return feedparser.parse(xml_text)


async def _fetch_feed_text(session: aiohttp.ClientSession, title: str, url: str) -> Optional[str]:
    """A single feed's own DNS lookup/connection can transiently fail (observed on this
    network — concurrent fan-out to many hosts occasionally makes a healthy host's
    resolution time out) without the site itself being down, so each feed gets a couple
    of tries before being given up on for this cycle."""
    for attempt in range(_FEED_RETRY_ATTEMPTS):
        try:
            async with session.get(url, headers={"User-Agent": _USER_AGENT}, timeout=_FEED_TIMEOUT) as resp:
                if resp.status != 200:
                    logger.warning("Newspaper feed '%s' returned HTTP %s", title, resp.status)
                else:
                    return await resp.text()
        except Exception as exc:
            logger.warning("Error fetching newspaper feed '%s' (attempt %d): %s", title, attempt + 1, exc)

        if attempt < _FEED_RETRY_ATTEMPTS - 1:
            await asyncio.sleep(_FEED_RETRY_DELAY_SECONDS)

    return None


async def fetch_newspaper_feed(session: aiohttp.ClientSession, title: str, url: str) -> list[FetchedPost]:
    """Fetches and parses a single newspaper RSS feed. Never raises — a slow/broken feed
    should never stop the other ~38 feeds in the same asyncio.gather batch from being read."""
    xml_text = await _fetch_feed_text(session, title, url)
    if xml_text is None:
        return []

    try:
        feed = await asyncio.to_thread(_parse_feed, xml_text)
    except Exception:
        logger.exception("Error parsing newspaper feed '%s'", title)
        return []

    if feed.bozo and not feed.entries:
        logger.warning("Newspaper feed '%s' could not be parsed: %s", title, feed.get("bozo_exception"))
        return []

    posts: list[FetchedPost] = []

    for entry in feed.entries[:POSTS_PER_FETCH]:
        link = entry.get("link")
        entry_title = (entry.get("title") or "").strip()
        if not link or not entry_title:
            continue

        external_id = entry.get("id") or link
        summary = entry.get("summary") or entry.get("description") or ""

        published_at = datetime.now(timezone.utc)
        parsed_time = entry.get("published_parsed") or entry.get("updated_parsed")
        if parsed_time:
            try:
                published_at = datetime(*parsed_time[:6], tzinfo=timezone.utc)
            except (TypeError, ValueError):
                pass

        posts.append(
            FetchedPost(
                external_id=external_id,
                title=entry_title,
                text=_clean_html(summary),
                url=link,
                published_at=published_at,
            )
        )

    return posts
