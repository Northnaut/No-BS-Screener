import logging
from datetime import timezone

from telethon import TelegramClient
from telethon.errors import FloodWaitError

from app.parsers.base import FetchedPost
from app.services import userbot

logger = logging.getLogger(__name__)


async def fetch_telegram_posts(client: TelegramClient, channel_username: str, limit: int = 25) -> list[FetchedPost]:
    remaining = userbot.flood_wait_remaining()
    if remaining > 0:
        raise RuntimeError(
            f"Telegram userbot is in a flood-wait cooldown ({remaining}s remaining), skipping @{channel_username}"
        )

    try:
        entity = await client.get_entity(channel_username)
        messages = await client.get_messages(entity, limit=limit)
    except FloodWaitError as exc:
        userbot.register_flood_wait(exc.seconds)
        logger.warning(
            "Telegram flood wait triggered by @%s: pausing all Telegram usage for %ds",
            channel_username, exc.seconds,
        )
        raise RuntimeError(f"Telegram flood wait ({exc.seconds}s) fetching @{channel_username}") from exc
    except Exception as exc:
        raise RuntimeError(f"Failed to fetch Telegram messages for @{channel_username}") from exc

    posts: list[FetchedPost] = []

    for message in messages:
        text = (message.message or "").strip()
        if not text:
            continue

        title = text.splitlines()[0][:200]
        published_at = message.date
        if published_at.tzinfo is None:
            published_at = published_at.replace(tzinfo=timezone.utc)

        posts.append(
            FetchedPost(
                external_id=f"tg:{message.id}",
                title=title,
                text=text,
                url=f"https://t.me/{channel_username}/{message.id}",
                published_at=published_at,
            )
        )

    return posts
