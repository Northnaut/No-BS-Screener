import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.config import BOT_TOKEN, POLL_INTERVAL_MINUTES
from app.database.schema import init_db
from app.handlers import register_handlers
from app.middlewares.dedup import DeduplicationMiddleware
from app.services.poller import run_classification_worker, run_cleanup, run_polling_cycle
from app.utils.logger import setup_logging

logger = logging.getLogger(__name__)


async def main() -> None:
    setup_logging()
    logger.info("Starting No BS Screener bot...")

    try:
        await init_db()
    except Exception:
        logger.exception("Failed to initialize database")
        raise

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(DeduplicationMiddleware())
    register_handlers(dp)

    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        run_polling_cycle, "interval", minutes=POLL_INTERVAL_MINUTES,
        args=[bot], id="discovery_cycle", max_instances=1, coalesce=True,
    )
    scheduler.add_job(run_cleanup, "interval", hours=24, id="cleanup")
    scheduler.start()
    logger.info("Scheduler started (discovery every %d minutes)", POLL_INTERVAL_MINUTES)

    classification_task = asyncio.create_task(run_classification_worker(bot))

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
        classification_task.cancel()
        try:
            await classification_task
        except asyncio.CancelledError:
            pass
        scheduler.shutdown(wait=False)
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped")
