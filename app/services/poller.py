import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiohttp
from aiogram import Bot
from telethon import TelegramClient

from app.ai.client import (
    ProvidersFailedError,
    QuotaExceededError,
    classify_posts_batch,
    summarize_posts_batch,
)
from app.config import (
    CLASSIFICATION_BATCH_MAX_POSTS,
    MAX_POST_AGE_HOURS,
    NEWSPAPER_QUEUE_MAX_PER_USER,
    NEWSPAPER_SOURCES_PER_CYCLE,
)
from app.database.queries import (
    bump_classification_attempts,
    claim_unseen_posts,
    enqueue_newspaper_delivery,
    get_all_active_sources,
    get_newspaper_category_subscribers,
    get_newspaper_sources,
    get_unclassified_posts,
    mark_source_checked,
    purge_old_seen_posts,
    save_seen_post,
)
from app.parsers.base import FetchedPost
from app.parsers.newspapers import fetch_newspaper_feed
from app.parsers.reddit import fetch_reddit_posts
from app.parsers.telegram import fetch_telegram_posts
from app.parsers.youtube import fetch_youtube_videos
from app.services.notifier import broadcast, broadcast_video

logger = logging.getLogger(__name__)

_MAX_CONCURRENT_NEWSPAPER_FETCHES = 6
_MAX_POST_AGE = timedelta(hours=MAX_POST_AGE_HOURS)


async def _fetch_source_posts(
    session: aiohttp.ClientSession, source: dict, telegram_client: Optional[TelegramClient] = None
) -> list[FetchedPost]:
    if source["platform"] == "reddit":
        return await fetch_reddit_posts(session, source["external_id"])
    if source["platform"] == "youtube":
        return await fetch_youtube_videos(session, source["external_id"])
    if source["platform"] == "telegram":
        if telegram_client is None:
            raise RuntimeError("Telegram userbot is not connected")
        return await fetch_telegram_posts(telegram_client, source["external_id"])
    if source["platform"] == "newspaper":
        return await fetch_newspaper_feed(session, source["title"] or source["external_id"], source["url"])
    logger.warning("Unknown platform '%s' for source %s", source["platform"], source["id"])
    return []


async def _discover_source(
    bot: Bot, session: aiohttp.ClientSession, source: dict, telegram_client: Optional[TelegramClient] = None
) -> None:
    """Fetch a source's feed. Reddit/Telegram posts are queued for AI triage; YouTube videos alert directly."""
    source_id = source["id"]
    platform = source["platform"]

    try:
        posts = await _fetch_source_posts(session, source, telegram_client)
    except RuntimeError as exc:
        # An expected, already-handled fetch failure that the parser raises itself. Logging
        # a full stack dump for these produced 2,000+ redundant tracebacks a day (over half
        # the entire log file) telling us nothing the message doesn't.
        logger.warning(
            "Failed to fetch posts for source %s (%s:%s): %s",
            source_id, platform, source["external_id"], exc,
        )
        await mark_source_checked(source_id, success=False)
        return
    except Exception:
        logger.exception("Failed to fetch posts for source %s (%s:%s)", source_id, platform, source["external_id"])
        await mark_source_checked(source_id, success=False)
        return

    try:
        claimed_ids = await claim_unseen_posts(source_id, posts)
    except Exception:
        logger.exception("Failed to claim posts for source %s (%s:%s)", source_id, platform, source["external_id"])
        await mark_source_checked(source_id, success=False)
        return

    new_posts = [post for post in posts if post.external_id in claimed_ids]

    if not source["is_bootstrapped"]:
        for post in new_posts:
            await save_seen_post(source_id, post.external_id, post.title, post.url, is_important=False)
        logger.info(
            "Bootstrapped source %s (%s:%s) with %d posts, no alerts sent",
            source_id, platform, source["external_id"], len(new_posts),
        )
        await mark_source_checked(source_id, success=True)
        return

    now = datetime.now(timezone.utc)
    fresh_posts = [post for post in new_posts if now - post.published_at <= _MAX_POST_AGE]
    stale_posts = [post for post in new_posts if now - post.published_at > _MAX_POST_AGE]
    if stale_posts:
        for post in stale_posts:
            await save_seen_post(source_id, post.external_id, post.title, post.url, is_important=False)
        logger.info(
            "Skipped %d stale post(s) (older than %dh) from source %s (%s:%s), no alert",
            len(stale_posts), MAX_POST_AGE_HOURS, source_id, platform, source["external_id"],
        )
    new_posts = fresh_posts

    if platform == "youtube":
        source_label = source["title"] or platform
        for post in new_posts:
            await save_seen_post(source_id, post.external_id, post.title, post.url, is_important=True, summary="")
            is_short = "/shorts/" in post.url
            try:
                await broadcast_video(bot, source_id, source_label, post.title, post.url, is_short=is_short)
            except Exception:
                logger.exception("Failed to broadcast video alert for '%s'", post.title)
        if new_posts:
            logger.info("Alerted %d new video(s) from source %s (%s:%s), no AI triage", len(new_posts), source_id, platform, source["external_id"])
    elif new_posts:
        logger.info("Queued %d new post(s) from source %s (%s:%s) for AI triage", len(new_posts), source_id, platform, source["external_id"])

    await mark_source_checked(source_id, success=True)


async def run_polling_cycle(bot: Bot, telegram_client: Optional[TelegramClient] = None) -> None:
    """Fast discovery pass: fetch all sources, queue Reddit/Telegram posts for AI triage, alert YouTube videos directly."""
    sources = await get_all_active_sources()
    if not sources:
        logger.info("No active sources to poll")
        return

    logger.info("Starting discovery cycle for %d source(s)", len(sources))

    async with aiohttp.ClientSession() as session:
        for source in sources:
            try:
                await _discover_source(bot, session, source, telegram_client)
            except Exception:
                logger.exception("Unexpected error discovering source %s", source["id"])
                continue

    logger.info("Discovery cycle finished")


async def run_newspaper_discovery_cycle(bot: Bot) -> None:
    """Newspapers are a fixed, pre-seeded list (not user-subscribed sources). The full list
    stays intact, but only a slice is actually fetched each cycle — the downstream
    classification batch (see run_classification_batch) only drains up to
    CLASSIFICATION_BATCH_MAX_POSTS newspaper posts per tick, so polling all ~39 feeds every
    cycle (each good for up to POSTS_PER_FETCH new posts) would queue far more than it can
    drain and the backlog of never-classified posts would grow without bound. Taking the
    least-recently-checked slice keeps each cycle's intake within what the batch job can
    keep up with, while guaranteeing every feed gets its turn in strict rotation. The slice
    is fetched concurrently via asyncio.gather — sequential fetching would make one slow site
    delay the whole cycle. Concurrency is capped by a semaphore: firing every site's DNS
    lookup at once was observed to make even healthy hosts' resolution time out."""
    sources = await get_newspaper_sources(limit=NEWSPAPER_SOURCES_PER_CYCLE)
    if not sources:
        logger.info("No newspaper sources to poll")
        return

    logger.info("Starting newspaper discovery cycle for %d source(s)", len(sources))
    semaphore = asyncio.Semaphore(_MAX_CONCURRENT_NEWSPAPER_FETCHES)

    async def _bounded_discover(session: aiohttp.ClientSession, source: dict) -> None:
        async with semaphore:
            await _discover_source(bot, session, source)

    async with aiohttp.ClientSession() as session:
        results = await asyncio.gather(
            *(_bounded_discover(session, source) for source in sources),
            return_exceptions=True,
        )

    for source, result in zip(sources, results):
        if isinstance(result, Exception):
            logger.exception("Unexpected error discovering newspaper source %s", source["id"], exc_info=result)

    logger.info("Newspaper discovery cycle finished")


async def _classify_newspaper_batch(posts: list[dict]) -> None:
    """Newspapers skip importance triage entirely — every post from this curated source
    list is always shown, only the style is AI-written."""
    batch_input = [
        {"id": i, "platform": post["platform"], "title": post["title"], "text": post["text"]}
        for i, post in enumerate(posts)
    ]
    try:
        results = await summarize_posts_batch(batch_input)
    except QuotaExceededError:
        logger.warning("All AI providers exhausted their quota, newspaper batch of %d post(s) will retry next cycle", len(posts))
        await bump_classification_attempts([p["seen_post_id"] for p in posts])
        return
    except ProvidersFailedError:
        logger.warning("All AI providers failed, newspaper batch of %d post(s) will retry next cycle", len(posts))
        await bump_classification_attempts([p["seen_post_id"] for p in posts])
        return
    except Exception:
        logger.exception("Unexpected error summarizing newspaper batch of %d post(s), will retry next cycle", len(posts))
        await bump_classification_attempts([p["seen_post_id"] for p in posts])
        return

    for i, post in enumerate(posts):
        summary = results[i]
        try:
            seen_post_id = await save_seen_post(
                post["source_id"], post["post_external_id"], post["title"], post["url"],
                is_important=True,
                summary=summary.summaries["brief"], summary_degen=summary.summaries["degen"],
                summary_eli5=summary.summaries["eli5"], summary_tiktok=summary.summaries["tiktok"],
                keep_text=True,
            )
        except Exception:
            logger.exception("Failed to save newspaper post '%s', will retry next cycle", post["title"])
            continue

        # Classified posts don't go out immediately — they're fanned out into each
        # subscriber's personal delivery queue, and a separate dispatcher (notifier.
        # dispatch_newspaper_alerts) drains it at a capped rate per user. With ~38
        # curated feeds discovered continuously, sending on classification would spam
        # a subscriber every few seconds; queueing decouples discovery/AI throughput
        # from how often any one person actually gets pinged.
        try:
            subscribers = await get_newspaper_category_subscribers(post["category"])
            for subscriber in subscribers:
                await enqueue_newspaper_delivery(
                    subscriber["id"], seen_post_id, post["category"], NEWSPAPER_QUEUE_MAX_PER_USER
                )
        except Exception:
            logger.exception("Failed to queue newspaper post '%s' for delivery", post["title"])


async def _classify_reddit_telegram_batch(bot: Bot, posts: list[dict]) -> None:
    batch_input = [
        {"id": i, "platform": post["platform"], "title": post["title"], "text": post["text"]}
        for i, post in enumerate(posts)
    ]
    try:
        results = await classify_posts_batch(batch_input)
    except QuotaExceededError:
        logger.warning("All AI providers exhausted their quota, reddit/telegram batch of %d post(s) will retry next cycle", len(posts))
        await bump_classification_attempts([p["seen_post_id"] for p in posts])
        return
    except ProvidersFailedError:
        logger.warning("All AI providers failed, reddit/telegram batch of %d post(s) will retry next cycle", len(posts))
        await bump_classification_attempts([p["seen_post_id"] for p in posts])
        return
    except Exception:
        logger.exception("Unexpected error classifying batch of %d post(s), will retry next cycle", len(posts))
        await bump_classification_attempts([p["seen_post_id"] for p in posts])
        return

    for i, post in enumerate(posts):
        result = results[i]
        try:
            await save_seen_post(
                post["source_id"], post["post_external_id"], post["title"], post["url"],
                is_important=result.is_important,
                summary=result.summaries["brief"], summary_degen=result.summaries["degen"],
                summary_eli5=result.summaries["eli5"], summary_tiktok=result.summaries["tiktok"],
            )
        except Exception:
            logger.exception("Failed to save classification result for post '%s', will retry next cycle", post["title"])
            continue

        # The importance filter's effect was previously unmeasurable — no verdict was ever
        # logged, so the classify -> send funnel could not be reconstructed from production
        # data at all.
        logger.info(
            "Verdict %s (%d/10) for %s post '%.80s': %s",
            "IMPORTANT" if result.is_important else "NOISE",
            result.score,
            post["platform"], post["title"], result.reason,
        )

        if result.is_important:
            source_label = post["source_title"] or post["platform"]
            try:
                await broadcast(
                    bot, post["source_id"], source_label, post["title"], post["text"], post["url"],
                    result.summaries,
                )
            except Exception:
                logger.exception("Failed to broadcast alert for post '%s'", post["title"])


async def run_classification_batch(bot: Bot) -> None:
    """Runs on a fixed interval (see main.py). Drains everything queued for AI triage since
    the last run in two batch calls — one for newspapers, one for reddit/telegram — instead
    of one AI request per post. Whatever doesn't fit within CLASSIFICATION_BATCH_MAX_POSTS
    per group just waits for the next cycle."""
    try:
        newspaper_posts = await get_unclassified_posts(("newspaper",), CLASSIFICATION_BATCH_MAX_POSTS)
        other_posts = await get_unclassified_posts(("reddit", "telegram"), CLASSIFICATION_BATCH_MAX_POSTS)
    except Exception:
        logger.exception("Failed to fetch unclassified posts for the classification batch")
        return

    if not newspaper_posts and not other_posts:
        return

    logger.info(
        "Classification batch: %d newspaper post(s), %d reddit/telegram post(s)",
        len(newspaper_posts), len(other_posts),
    )

    if newspaper_posts:
        await _classify_newspaper_batch(newspaper_posts)

    if other_posts:
        await _classify_reddit_telegram_batch(bot, other_posts)


async def run_cleanup() -> None:
    removed = await purge_old_seen_posts(older_than_days=30)
    if removed:
        logger.info("Purged %d old seen_posts entries", removed)
