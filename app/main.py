import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.config import (
    BOT_TOKEN,
    CLASSIFICATION_BATCH_INTERVAL_MINUTES,
    NEWSPAPER_ALERT_INTERVAL_MINUTES,
    NEWSPAPER_POLL_INTERVAL_MINUTES,
    POLL_INTERVAL_MINUTES,
)
from app.database.connection import close_connection_pragmas, init_connection_pragmas
from app.database.schema import init_db
from app.handlers import register_handlers
from app.middlewares.dedup import DeduplicationMiddleware
from app.parsers.newspapers import seed_newspaper_sources
from app.services.notifier import dispatch_newspaper_alerts, run_outgoing_dispatcher
from app.services.poller import (
    run_classification_batch,
    run_cleanup,
    run_newspaper_discovery_cycle,
    run_polling_cycle,
)
from app.services.userbot import start_userbot, stop_userbot
from app.utils.logger import cleanup_old_logs, setup_logging

logger = logging.getLogger(__name__)


async def main() -> None:
    setup_logging()
    logger.info("Starting No BS Screener bot...")

    try:
        await init_connection_pragmas()
        await init_db()
    except Exception:
        logger.exception("Failed to initialize database")
        raise

    try:
        await seed_newspaper_sources()
    except Exception:
        logger.exception("Failed to seed newspaper sources")
        raise

    telegram_client = await start_userbot()

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(DeduplicationMiddleware())
    register_handlers(dp)

    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        run_polling_cycle, "interval", minutes=POLL_INTERVAL_MINUTES,
        args=[bot, telegram_client], id="discovery_cycle", max_instances=1, coalesce=True,
    )
    scheduler.add_job(
        run_newspaper_discovery_cycle, "interval", minutes=NEWSPAPER_POLL_INTERVAL_MINUTES,
        args=[bot], id="newspaper_discovery_cycle", max_instances=1, coalesce=True,
    )
    # Ticks every minute so a user's per-interval throttle is honored promptly, but each
    # tick only pops at most one queued post per due user — the actual pacing is enforced
    # by NEWSPAPER_ALERT_INTERVAL_MINUTES via users.last_newspaper_alert_at, not by this
    # job's own frequency.
    scheduler.add_job(
        dispatch_newspaper_alerts, "interval", minutes=1,
        args=[bot, NEWSPAPER_ALERT_INTERVAL_MINUTES], id="newspaper_alert_dispatch",
        max_instances=1, coalesce=True,
    )
    scheduler.add_job(
        run_classification_batch, "interval", minutes=CLASSIFICATION_BATCH_INTERVAL_MINUTES,
        args=[bot], id="classification_batch", max_instances=1, coalesce=True,
    )
    scheduler.add_job(run_cleanup, "interval", hours=24, id="cleanup")
    scheduler.add_job(cleanup_old_logs, "interval", hours=1, id="log_cleanup")
    scheduler.start()

    outgoing_dispatcher_task = asyncio.create_task(run_outgoing_dispatcher())
    logger.info(
        "Scheduler started (discovery every %d minutes, newspapers every %d minutes, "
        "classification batched every %d minutes, newspaper alerts throttled to 1 per %d minutes per user)",
        POLL_INTERVAL_MINUTES, NEWSPAPER_POLL_INTERVAL_MINUTES,
        CLASSIFICATION_BATCH_INTERVAL_MINUTES, NEWSPAPER_ALERT_INTERVAL_MINUTES,
    )

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await bot.set_my_commands([
            BotCommand(command="start", description="Open main menu"),
            BotCommand(command="help", description="How it works"),
        ])
        logger.info("Bot polling started")
        await dp.start_polling(bot)
    except Exception:
        logger.exception("Bot polling crashed")
        raise
    finally:
        scheduler.shutdown(wait=False)
        outgoing_dispatcher_task.cancel()
        try:
            await outgoing_dispatcher_task
        except asyncio.CancelledError:
            pass
        await bot.session.close()
        await stop_userbot()
        await close_connection_pragmas()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped")
