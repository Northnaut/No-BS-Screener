import logging
from pathlib import Path

from aiogram import Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import FSInputFile, Message

from app.database.queries import get_or_create_user
from app.keyboards.inline import main_menu_keyboard

logger = logging.getLogger(__name__)
router = Router()

LOGO_PATH = Path(__file__).resolve().parent.parent / "assets" / "logo.jpg"

WELCOME_TEXT = (
    "👋 Welcome to <b>No BS Screener</b>!\n\n"
    "I watch Reddit, YouTube, Telegram, and curated newspapers so you don't have to, "
    "then let AI cut the noise — you only hear about stuff that's actually big.\n\n"
    "Use the menu below to add a source."
)


async def send_main_menu(message: Message) -> Message:
    return await message.answer_photo(
        FSInputFile(LOGO_PATH),
        caption=WELCOME_TEXT,
        reply_markup=main_menu_keyboard(),
    )


@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    try:
        await get_or_create_user(message.from_user.id, message.from_user.username)
    except Exception:
        logger.exception("Failed to register user %s", message.from_user.id)
        await message.answer("⚠️ Something went wrong while setting up your account. Please try again.")
        return

    await send_main_menu(message)


@router.message(Command("menu"))
async def cmd_menu(message: Message, state: FSMContext) -> None:
    await state.clear()
    await send_main_menu(message)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        "ℹ️ <b>How it works:</b>\n\n"
        "1. Add a Reddit, YouTube, Telegram source via the menu — "
        "you can paste several links at once to add them in bulk.\n"
        "2. I periodically check every source for new posts, videos, and articles.\n"
        "3. AI filters out noise and memes, judging importance by the real-world scale "
        "of the actor involved — not by keywords or topic, so it works for any subject.\n"
        "4. You get notified only about genuinely important news.\n\n"
        "🎨 <b>Summary styles</b> — pick how alerts are written: Original, TL;DR, Casual, "
        "ELI5, or TikTok. Change it anytime in 🎨 Styles.\n"
        "📰 <b>Newspapers</b> — subscribe to whole news categories, not just individual feeds.\n"
        "Commands:\n"
        "/start — main menu\n"
        "/help — this message"
    )
