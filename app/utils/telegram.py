import logging

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardMarkup, Message

logger = logging.getLogger(__name__)


async def safe_edit_text(message: Message, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> None:
    try:
        await message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc):
            return
        if "there is no text in the message to edit" in str(exc):
            # message being edited is a photo/media message (e.g. the /start welcome
            # screen with the logo) which has no .text to edit, only a .caption.
            await message.answer(text, reply_markup=reply_markup)
            return
        raise
