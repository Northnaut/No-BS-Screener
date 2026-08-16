import asyncio
import logging
from typing import Optional

import aiohttp
from aiogram import Bot
from telethon import TelegramClient

from app.ai.client import QuotaExceededError, classify_post
from app.database.queries import (
    claim_post_if_unseen,
    get_all_active_sources,
    get_next_unclassified_post,
    mark_source_checked,
    purge_old_seen_posts,
    save_seen_post,
)
from app.parsers.base import FetchedPost
from app.parsers.reddit import fetch_reddit_posts
from app.parsers.telegram import fetch_telegram_posts
from app.parsers.youtube import fetch_youtube_videos
from app.services.notifier import broadcast, broadcast_video

logger = logging.getLogger(__name__)

_GEMINI_CALL_DELAY_SECONDS = 13
_EMPTY_QUEUE_POLL_SECONDS = 5
_QUOTA_BACKOFF_SECONDS = 30


async def _fetch_source_posts(
    session: aiohttp.ClientSession, source: dict, telegram_client: Optional[TelegramClient] = None
) -> list[FetchedPost]:
    if source["platform"] == "reddit":
        return await fetch_reddit_posts(session, source["external_id"])
    if source["platform"] == "youtube":
        return await fetch_youtube_videos(source["external_id"])
    if source["platform"] == "telegram":
        if telegram_client is None:
            raise RuntimeError("Telegram userbot is not connected")
        return await fetch_telegram_posts(telegram_client, source["external_id"])
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
    except Exception:
        logger.exception("Failed to fetch posts for source %s (%s:%s)", source_id, platform, source["external_id"])
        await mark_source_checked(source_id, success=False)
        return

    new_posts = []
    for post in posts:
        claimed = await claim_post_if_unseen(source_id, post.external_id, post.title, post.text, post.url)
        if claimed:
            new_posts.append(post)

    if not source["is_bootstrapped"]:
        for post in new_posts:
            await save_seen_post(source_id, post.external_id, post.title, post.url, is_important=False)
        logger.info(
            "Bootstrapped source %s (%s:%s) with %d posts, no alerts sent",
            source_id, platform, source["external_id"], len(new_posts),
        )
        await mark_source_checked(source_id, success=True)
        return

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


async def run_classification_worker(bot: Bot) -> None:
    """Continuously drains the classification queue, one post at a time, as fast as the AI quota allows."""
    logger.info("Classification worker started")

    while True:
        try:
            post = await get_next_unclassified_post()
        except Exception:
            logger.exception("Failed to fetch next unclassified post")
            await asyncio.sleep(_EMPTY_QUEUE_POLL_SECONDS)
            continue

        if post is None:
            await asyncio.sleep(_EMPTY_QUEUE_POLL_SECONDS)
            continue

        try:
            result = await classify_post(post["platform"], post["title"], post["text"])
        except QuotaExceededError:
            logger.warning("Gemini quota exhausted, backing off %ds before retrying", _QUOTA_BACKOFF_SECONDS)
            await asyncio.sleep(_QUOTA_BACKOFF_SECONDS)
            continue
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Unexpected error classifying post '%s', skipping it", post["title"])
            try:
                await save_seen_post(post["source_id"], post["post_external_id"], post["title"], post["url"], is_important=False)
            except Exception:
                logger.exception("Failed to save fallback result for post '%s', will retry next cycle", post["title"])
            await asyncio.sleep(_GEMINI_CALL_DELAY_SECONDS)
            continue

        try:
            await save_seen_post(
                post["source_id"], post["post_external_id"], post["title"], post["url"],
                is_important=result.is_important, summary=result.summary,
            )
        except Exception:
            logger.exception("Failed to save classification result for post '%s', will retry next cycle", post["title"])
            await asyncio.sleep(_GEMINI_CALL_DELAY_SECONDS)
            continue

        if result.is_important:
            source_label = post["source_title"] or post["platform"]
            try:
                await broadcast(bot, post["source_id"], source_label, post["title"], post["url"], result.summary)
            except Exception:
                logger.exception("Failed to broadcast alert for post '%s'", post["title"])

        await asyncio.sleep(_GEMINI_CALL_DELAY_SECONDS)


async def run_cleanup() -> None:
    removed = await purge_old_seen_posts(older_than_days=30)
    if removed:
        logger.info("Purged %d old seen_posts entries", removed)
