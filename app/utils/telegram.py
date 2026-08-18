import logging
from pathlib import Path

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import FSInputFile, InlineKeyboardMarkup, InputMediaPhoto, Message

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
            # Delete it before sending the replacement — otherwise it's orphaned in
            # the chat and resurfaces as a stray duplicate screen later.
            try:
                await message.delete()
            except TelegramBadRequest:
                pass
            await message.answer(text, reply_markup=reply_markup)
            return
        raise


async def safe_edit_photo(
    message: Message, photo_path: Path, caption: str, reply_markup: InlineKeyboardMarkup | None = None
) -> None:
    try:
        await message.edit_media(
            media=InputMediaPhoto(media=FSInputFile(photo_path), caption=caption),
            reply_markup=reply_markup,
        )
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc):
            return
        # message being edited is a plain text message, or a photo message aiogram/Telegram
        # won't let us swap media on (e.g. it wasn't the one we just sent) — delete it before
        # sending the replacement, same reasoning as safe_edit_text's fallback.
        try:
            await message.delete()
        except TelegramBadRequest:
            pass
        await message.answer_photo(FSInputFile(photo_path), caption=caption, reply_markup=reply_markup)
