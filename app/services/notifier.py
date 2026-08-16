import asyncio
import logging
from typing import Optional

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import LinkPreviewOptions

from app.database.queries import deactivate_user, get_source_subscribers
from app.utils.formatters import format_alert, format_video_alert

logger = logging.getLogger(__name__)

_SEND_DELAY_SECONDS = 0.05


async def _send_to_subscribers(
    bot: Bot, source_id: int, text: str,
    link_preview_options: Optional[LinkPreviewOptions] = None, is_short: bool = False,
) -> None:
    subscribers = await get_source_subscribers(source_id, is_short=is_short)
    if not subscribers:
        return

    for subscriber in subscribers:
        tg_id = subscriber["tg_id"]
        try:
            await bot.send_message(tg_id, text, link_preview_options=link_preview_options)
        except TelegramForbiddenError:
            logger.info("User %s blocked the bot, deactivating", tg_id)
            try:
                await deactivate_user(tg_id)
            except Exception:
                logger.exception("Failed to deactivate user %s in database", tg_id)
        except TelegramRetryAfter as exc:
            logger.warning("Rate limited sending to user %s, waiting %ss", tg_id, exc.retry_after)
            await asyncio.sleep(exc.retry_after)
            try:
                await bot.send_message(tg_id, text, link_preview_options=link_preview_options)
            except Exception:
                logger.exception("Failed to send alert to user %s after rate-limit wait", tg_id)
        except Exception:
            logger.exception("Failed to send alert to user %s", tg_id)

        await asyncio.sleep(_SEND_DELAY_SECONDS)


async def broadcast(
    bot: Bot, source_id: int, source_label: str, title: str, original_text: str, url: str,
    summary_brief: str, summary_degen: str,
) -> None:
    text = format_alert(source_label, title, original_text, summary_brief, summary_degen, url)
    await _send_to_subscribers(bot, source_id, text)


async def broadcast_video(bot: Bot, source_id: int, source_label: str, title: str, url: str, is_short: bool = False) -> None:
    text = format_video_alert(source_label, title, url)
    preview_options = LinkPreviewOptions(url=url, prefer_large_media=True, show_above_text=True)
    await _send_to_subscribers(bot, source_id, text, link_preview_options=preview_options, is_short=is_short)
