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
    "I track subreddits and YouTube channels for you and use AI to filter out "
    "noise — you only get notified about news that could actually move the market.\n\n"
    "Use the menu below to add a source."
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

    await message.answer_photo(
        FSInputFile(LOGO_PATH),
        caption=WELCOME_TEXT,
        reply_markup=main_menu_keyboard(),
    )


@router.message(Command("menu"))
async def cmd_menu(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Main menu:", reply_markup=main_menu_keyboard())


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        "ℹ️ <b>How it works:</b>\n\n"
        "1. Add a Reddit, YouTube, or Telegram source via the menu.\n"
        "2. I periodically check it for new posts/videos.\n"
        "3. AI filters out noise and memes.\n"
        "4. You get notified only about genuinely important news.\n\n"
        "Commands:\n"
        "/start — main menu\n"
        "/menu — main menu\n"
        "/help — this message"
    )
