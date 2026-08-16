import logging
import time
from typing import Optional

from telethon import TelegramClient
from telethon.sessions import StringSession

from app.config import TG_API_HASH, TG_API_ID, TG_SESSION_STRING

logger = logging.getLogger(__name__)

_client: Optional[TelegramClient] = None

# Shared across every caller (poller fetches, link validation, bulk-add) since a
# FloodWaitError on this account blocks the whole session regardless of which
# call triggered it.
_flood_wait_until = 0.0


def flood_wait_remaining() -> int:
    """Seconds left in an active Telegram flood-wait cooldown, 0 if none."""
    return max(0, int(_flood_wait_until - time.monotonic()))


def register_flood_wait(seconds: float) -> None:
    global _flood_wait_until
    _flood_wait_until = time.monotonic() + seconds


def is_userbot_configured() -> bool:
    return bool(TG_API_ID and TG_API_HASH and TG_SESSION_STRING)


async def start_userbot() -> Optional[TelegramClient]:
    """Connects the Telegram userbot using a pre-generated session string.

    Never calls client.start() here — that can prompt for a login code
    interactively, which would hang the bot process. Run
    scripts/telegram_login.py once to generate TG_SESSION_STRING instead.
    """
    global _client

    if not is_userbot_configured():
        logger.warning(
            "Telegram userbot not configured (TG_API_ID/TG_API_HASH/TG_SESSION_STRING missing). "
            "Telegram channel sources will be skipped. Run scripts/telegram_login.py to set it up."
        )
        return None

    try:
        client = TelegramClient(StringSession(TG_SESSION_STRING), int(TG_API_ID), TG_API_HASH)
        await client.connect()

        if not await client.is_user_authorized():
            logger.error(
                "Telegram userbot session is invalid or expired. Re-run scripts/telegram_login.py "
                "and update TG_SESSION_STRING."
            )
            await client.disconnect()
            return None
    except Exception:
        logger.exception(
            "Failed to start Telegram userbot (check TG_API_ID/TG_API_HASH/TG_SESSION_STRING). "
            "Telegram channel sources will be skipped."
        )
        return None

    _client = client
    logger.info("Telegram userbot connected")
    return client


async def stop_userbot() -> None:
    global _client
    if _client is not None:
        await _client.disconnect()
        _client = None


def get_userbot_client() -> Optional[TelegramClient]:
    return _client
