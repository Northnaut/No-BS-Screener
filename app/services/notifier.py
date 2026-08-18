import asyncio
import logging
from typing import Callable, Optional

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import LinkPreviewOptions

from app.config import OUTGOING_ALERT_INTERVAL_SECONDS
from app.database.queries import (
    deactivate_user,
    delete_newspaper_delivery,
    get_source_subscribers,
    get_users_due_for_newspaper_alert,
    peek_next_newspaper_delivery,
    update_last_newspaper_alert,
)
from app.utils.formatters import format_alert, format_newspaper_alert, format_video_alert

logger = logging.getLogger(__name__)

_SEND_DELAY_SECONDS = 0.05

# Reddit/Telegram/YouTube alerts (newspapers have their own separate per-user throttled
# queue, see dispatch_newspaper_alerts below) go through this shared in-memory queue instead
# of sending the moment a post is classified important. run_outgoing_dispatcher drains it one
# post at a time, at least OUTGOING_ALERT_INTERVAL_SECONDS apart, so a backlog freed all at
# once doesn't land in a subscriber's chat within seconds. In-memory by design, like
# _delivery_failures below — a restart dropping whatever's still queued is the safe direction,
# not a bug to fix.
_OutgoingJob = tuple[Bot, list[dict], Callable[[dict], str], Optional[LinkPreviewOptions]]
_outgoing_queue: "asyncio.Queue[_OutgoingJob]" = asyncio.Queue()

# Consecutive failed send attempts per queue item, kept in memory only — a restart clearing
# these just means a fresh set of retries, which is the safe direction.
_MAX_DELIVERY_FAILURES = 3
_delivery_failures: dict[int, int] = {}


async def _send_one(
    bot: Bot, tg_id: int, text: str, link_preview_options: Optional[LinkPreviewOptions] = None,
) -> bool:
    """Returns True only if the message actually reached Telegram. Callers that consume a
    queued item need to know — silently returning None meant a failed send still counted as
    a delivery, destroying the item and burning the user's throttle slot."""
    try:
        await bot.send_message(tg_id, text, link_preview_options=link_preview_options)
        return True
    except TelegramForbiddenError:
        logger.info("User %s blocked the bot, deactivating", tg_id)
        try:
            await deactivate_user(tg_id)
        except Exception:
            logger.exception("Failed to deactivate user %s in database", tg_id)
        # Deliberately True: the user is gone, so re-queueing the item would just retry
        # forever against a dead chat.
        return True
    except TelegramRetryAfter as exc:
        logger.warning("Rate limited sending to user %s, waiting %ss", tg_id, exc.retry_after)
        await asyncio.sleep(exc.retry_after)
        try:
            await bot.send_message(tg_id, text, link_preview_options=link_preview_options)
            return True
        except Exception:
            logger.exception("Failed to send alert to user %s after rate-limit wait", tg_id)
            return False
    except Exception:
        logger.exception("Failed to send alert to user %s", tg_id)
        return False


async def _send(
    bot: Bot, subscribers: list[dict], text_for: Callable[[dict], str],
    link_preview_options: Optional[LinkPreviewOptions] = None,
) -> None:
    if not subscribers:
        return

    for subscriber in subscribers:
        await _send_one(bot, subscriber["tg_id"], text_for(subscriber), link_preview_options)
        await asyncio.sleep(_SEND_DELAY_SECONDS)


async def broadcast(
    bot: Bot, source_id: int, source_label: str, title: str, original_text: str, url: str,
    summaries: dict[str, str],
) -> None:
    def text_for(subscriber: dict) -> str:
        style = subscriber.get("summary_style") or "brief"
        return format_alert(style, source_label, title, original_text, summaries, url)

    subscribers = await get_source_subscribers(source_id)
    if not subscribers:
        return
    await _outgoing_queue.put((bot, subscribers, text_for, None))


async def broadcast_video(bot: Bot, source_id: int, source_label: str, title: str, url: str, is_short: bool = False) -> None:
    text = format_video_alert(source_label, title, url)
    preview_options = LinkPreviewOptions(url=url, prefer_large_media=True, show_above_text=True)
    subscribers = await get_source_subscribers(source_id, is_short=is_short)
    if not subscribers:
        return
    await _outgoing_queue.put((bot, subscribers, lambda _sub: text, preview_options))


async def run_outgoing_dispatcher() -> None:
    """Long-running background task (started once in main.py) that is the only thing which
    ever pulls from _outgoing_queue. Blocks on the queue when empty, sends the next post to
    all of its subscribers the moment one is queued, then waits OUTGOING_ALERT_INTERVAL_SECONDS
    before touching the next one — that wait is what actually enforces the pacing, independent
    of how many posts got queued back to back."""
    while True:
        bot, subscribers, text_for, link_preview_options = await _outgoing_queue.get()
        try:
            await _send(bot, subscribers, text_for, link_preview_options)
        except Exception:
            logger.exception("Failed to dispatch a queued alert")
        finally:
            _outgoing_queue.task_done()
        await asyncio.sleep(OUTGOING_ALERT_INTERVAL_SECONDS)


async def dispatch_newspaper_alerts(bot: Bot, interval_minutes: int) -> None:
    """Runs on a short tick (see main.py) and drains each due user's personal newspaper
    queue by exactly one post — this is the per-user throttle: no subscriber gets a
    newspaper alert more often than once per interval_minutes, no matter how many curated
    feeds fire off in the background. Posts that don't get popped this tick just wait for
    the next one (or the next-next one), they aren't lost."""
    due_users = await get_users_due_for_newspaper_alert(interval_minutes)
    if not due_users:
        return

    for user in due_users:
        try:
            item = await peek_next_newspaper_delivery(user["id"])
        except Exception:
            logger.exception("Failed to read newspaper delivery queue for user %s", user["id"])
            continue

        if item is None:
            continue

        style = user.get("summary_style") or "brief"
        source_label = item["source_title"] or "Newspaper"
        summaries = {
            "brief": item["summary"] or "",
            "degen": item["summary_degen"] or "",
            "eli5": item["summary_eli5"] or "",
            "tiktok": item["summary_tiktok"] or "",
        }
        text = format_newspaper_alert(
            style, source_label, item["title"], item["original_text"] or "", summaries, item["url"]
        )

        queue_id = item["queue_id"]
        delivered = await _send_one(bot, user["tg_id"], text)

        if not delivered:
            # Keep the item queued so a transient failure doesn't lose it, but don't let a
            # permanently unsendable post (e.g. malformed markup) block the queue head and
            # retry every tick forever.
            failures = _delivery_failures.get(queue_id, 0) + 1
            if failures >= _MAX_DELIVERY_FAILURES:
                logger.error(
                    "Dropping newspaper item %s for user %s after %d failed send attempts",
                    queue_id, user["id"], failures,
                )
                _delivery_failures.pop(queue_id, None)
                try:
                    await delete_newspaper_delivery(queue_id)
                except Exception:
                    logger.exception("Failed to drop newspaper delivery %s", queue_id)
            else:
                _delivery_failures[queue_id] = failures
                logger.warning(
                    "Newspaper item %s for user %s not delivered (attempt %d), keeping it queued",
                    queue_id, user["id"], failures,
                )
            continue

        _delivery_failures.pop(queue_id, None)
        try:
            await delete_newspaper_delivery(queue_id)
        except Exception:
            logger.exception("Failed to remove delivered newspaper item %s from the queue", queue_id)

        try:
            await update_last_newspaper_alert(user["id"])
        except Exception:
            logger.exception("Failed to update last_newspaper_alert_at for user %s", user["id"])

        # Delivery was previously invisible in the logs, which made the per-user cadence
        # impossible to verify from production data.
        logger.info(
            "Newspaper alert sent to user %s (queue item %s, category '%s', source '%s')",
            user["id"], queue_id, item["category"], source_label,
        )

        await asyncio.sleep(_SEND_DELAY_SECONDS)
