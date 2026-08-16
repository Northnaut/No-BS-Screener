import logging

from aiogram import Router
from aiogram.types import ErrorEvent

logger = logging.getLogger(__name__)
router = Router()


@router.error()
async def global_error_handler(event: ErrorEvent) -> bool:
    logger.exception(
        "Unhandled exception while processing update %s: %s",
        event.update.update_id,
        event.exception,
        exc_info=event.exception,
    )

    update = event.update
    target_message = update.message or (update.callback_query.message if update.callback_query else None)

    if target_message:
        try:
            await target_message.answer("⚠️ Something went wrong. Please try again or use /menu to restart.")
        except Exception:
            logger.exception("Failed to notify user about the error")

    return True
