import asyncio
import html
import logging
import re
import time
from pathlib import Path
from typing import Optional

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from app.handlers.commands import send_main_menu
from app.database.queries import (
    add_subscription,
    delete_all_subscriptions,
    delete_subscription,
    get_newspaper_categories,
    get_or_create_source,
    get_or_create_user,
    get_summary_style,
    get_user_subscriptions,
    get_youtube_shorts_enabled,
    set_summary_style,
    toggle_newspaper_category,
    toggle_youtube_shorts,
)
from app.keyboards.inline import (
    cancel_keyboard,
    confirm_unsubscribe_all_keyboard,
    main_menu_keyboard,
    newspaper_sources_keyboard,
    newspapers_keyboard,
    platform_keyboard,
    styles_keyboard,
    unsubscribe_items_keyboard,
)
from app.parsers.newspapers import CATEGORY_LABELS
from app.parsers.validators import (
    ValidatedSource,
    validate_reddit_link,
    validate_telegram_link,
    validate_youtube_link,
)
from app.services.userbot import get_userbot_client
from app.states.add_source import AddSourceStates
from app.utils.formatters import PLATFORM_LABELS, format_newspaper_sources, format_subscriptions_list
from app.utils.telegram import safe_edit_photo, safe_edit_text

NEWSPAPERS_IMAGE_PATH = Path(__file__).resolve().parent.parent / "assets" / "newspapers.jpg"

_STYLES_TEXT = (
    "Choose how you want AI alerts written (Reddit/Telegram/Newspapers — YouTube videos don't use AI):\n\n"
    "📰 <b>Original</b> — the raw post text, no AI rewrite\n"
    "⚡ <b>TL;DR</b> — one dry, factual sentence\n"
    "💬 <b>Casual</b> — simple, casual language, keeps the jargon, no corporate fluff\n"
    "🧒 <b>ELI5</b> — explained like you're 5, dead simple (and a little silly)\n"
    "🎵 <b>TikTok</b> — written in TikTok/Gen-Z slang"
)

_NEWSPAPERS_TEXT = (
    "📰 <b>Newspapers</b>\n\n"
    "Pick any categories you want news alerts from — you can select several. "
    "These sources are pre-vetted, so there's no importance filter: every article "
    "gets sent, AI only rewrites it into your chosen style."
)

logger = logging.getLogger(__name__)
router = Router()

_BULK_MAX_ITEMS = 30
_BULK_ITEM_DELAY_SECONDS = 0.5

_main_menu_last_shown: dict[int, float] = {}
_MAIN_MENU_DEBOUNCE_SECONDS = 3.0


@router.callback_query(F.data == "menu:main")
async def show_main_menu(callback: CallbackQuery, state: FSMContext) -> None:
    # Ack immediately: the delete+resend below takes a moment (photo upload).
    await callback.answer()

    chat_id = callback.message.chat.id if callback.message else callback.from_user.id
    now = time.monotonic()
    last_shown = _main_menu_last_shown.get(chat_id, 0.0)
    if now - last_shown < _MAIN_MENU_DEBOUNCE_SECONDS:
        # Defense in depth: collapse any repeat "menu:main" trigger for the same
        # chat that lands within the debounce window into a no-op.
        return
    _main_menu_last_shown[chat_id] = now

    await state.clear()
    try:
        await callback.message.delete()
    except TelegramBadRequest as exc:
        logger.debug("Could not delete message before showing main menu: %s", exc)
    await send_main_menu(callback.message)


@router.callback_query(F.data == "menu:styles")
async def show_styles(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    current = await get_summary_style(callback.from_user.id)
    await safe_edit_text(callback.message, _STYLES_TEXT, reply_markup=styles_keyboard(current))
    await callback.answer()


@router.callback_query(F.data.startswith("set_style:"))
async def set_style_handler(callback: CallbackQuery) -> None:
    style = callback.data.split(":")[1]
    try:
        await set_summary_style(callback.from_user.id, style)
    except Exception:
        logger.exception("Failed to save summary style for user %s", callback.from_user.id)
        await callback.answer("⚠️ Something went wrong.", show_alert=True)
        return
    await safe_edit_text(callback.message, _STYLES_TEXT, reply_markup=styles_keyboard(style))
    await callback.answer(f"Style set to {style}")


@router.callback_query(F.data == "menu:newspapers")
async def show_newspapers(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    try:
        user_id = await get_or_create_user(callback.from_user.id, callback.from_user.username)
        categories = await get_newspaper_categories(user_id)
    except Exception:
        logger.exception("Failed to load newspaper categories for user %s", callback.from_user.id)
        await callback.answer("⚠️ Something went wrong.", show_alert=True)
        return
    await safe_edit_photo(
        callback.message, NEWSPAPERS_IMAGE_PATH, _NEWSPAPERS_TEXT, reply_markup=newspapers_keyboard(categories)
    )
    await callback.answer()


@router.callback_query(F.data.startswith("toggle_newspaper_cat:"))
async def toggle_newspaper_category_handler(callback: CallbackQuery) -> None:
    category = callback.data.split(":")[1]
    try:
        user_id = await get_or_create_user(callback.from_user.id, callback.from_user.username)
        now_enabled = await toggle_newspaper_category(user_id, category)
        categories = await get_newspaper_categories(user_id)
    except Exception:
        logger.exception("Failed to toggle newspaper category '%s' for user %s", category, callback.from_user.id)
        await callback.answer("⚠️ Something went wrong.", show_alert=True)
        return

    await safe_edit_photo(
        callback.message, NEWSPAPERS_IMAGE_PATH, _NEWSPAPERS_TEXT, reply_markup=newspapers_keyboard(categories)
    )
    label = CATEGORY_LABELS.get(category, category)
    await callback.answer(f"{label}: {'ON' if now_enabled else 'OFF'}")


@router.callback_query(F.data == "newspaper_sources")
async def show_newspaper_sources(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await safe_edit_text(callback.message, format_newspaper_sources(), reply_markup=newspaper_sources_keyboard())
    await callback.answer()


async def _render_platform_view(user_tg_id: int, username: Optional[str], platform: str) -> tuple[str, InlineKeyboardMarkup]:
    user_id = await get_or_create_user(user_tg_id, username)
    subscriptions = await get_user_subscriptions(user_id)
    filtered = [sub for sub in subscriptions if sub["platform"] == platform]

    shorts_enabled = True
    if platform == "youtube":
        shorts_enabled = await get_youtube_shorts_enabled(user_tg_id)

    text = format_subscriptions_list(filtered, platform)
    keyboard = platform_keyboard(platform, bool(filtered), shorts_enabled)
    return text, keyboard


@router.callback_query(F.data.in_({"platform:reddit", "platform:youtube", "platform:telegram"}))
async def show_platform(callback: CallbackQuery, state: FSMContext) -> None:
    platform = callback.data.split(":")[1]
    await state.clear()
    try:
        text, keyboard = await _render_platform_view(callback.from_user.id, callback.from_user.username, platform)
    except Exception:
        logger.exception("Failed to load %s subscriptions for user %s", platform, callback.from_user.id)
        await callback.answer("⚠️ Failed to load subscriptions.", show_alert=True)
        return
    await safe_edit_text(callback.message, text, reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data == "toggle_shorts")
async def toggle_shorts_handler(callback: CallbackQuery) -> None:
    new_state = await toggle_youtube_shorts(callback.from_user.id)
    try:
        text, keyboard = await _render_platform_view(callback.from_user.id, callback.from_user.username, "youtube")
    except Exception:
        logger.exception("Failed to re-render subscriptions after toggling shorts for user %s", callback.from_user.id)
        await callback.answer("⚠️ Failed to update.", show_alert=True)
        return
    await safe_edit_text(callback.message, text, reply_markup=keyboard)
    await callback.answer(f"YouTube Shorts: {'ON' if new_state else 'OFF'}")


@router.callback_query(F.data == "noop")
async def noop_callback(callback: CallbackQuery) -> None:
    await callback.answer()


@router.callback_query(F.data.in_({"add:reddit", "add:youtube", "add:telegram"}))
async def start_add_source(callback: CallbackQuery, state: FSMContext) -> None:
    platform = callback.data.split(":")[1]
    await state.update_data(platform=platform)
    await state.set_state(AddSourceStates.waiting_for_link)

    if platform == "reddit":
        prompt = (
            "Send me a Reddit link.\n\n"
            "Example:\n<code>https://www.reddit.com/r/CryptoCurrency/</code>"
        )
    elif platform == "youtube":
        prompt = (
            "Send me a YouTube channel link.\n\n"
            "Example:\n<code>https://www.youtube.com/@MrBeast</code>"
        )
    else:
        prompt = (
            "Send me a Telegram channel link or @username.\n\n"
            "Example:\n<code>@CoinDesk</code> or <code>https://t.me/CoinDesk</code>"
        )

    prompt += f"\n\n💡 You can send several at once (one per line, up to {_BULK_MAX_ITEMS}) and I'll add them all."

    await safe_edit_text(
        callback.message,
        prompt,
        reply_markup=cancel_keyboard(f"platform:{platform}"),
    )
    await callback.answer()


def _split_bulk_input(raw: str) -> list[str]:
    tokens: list[str] = []
    for line in raw.splitlines():
        for piece in re.split(r"[,\s]+", line.strip()):
            piece = piece.strip()
            if piece:
                tokens.append(piece)
    return tokens


async def _validate_for_platform(platform: str, raw_item: str) -> Optional[ValidatedSource]:
    if platform == "reddit":
        return await validate_reddit_link(raw_item)
    if platform == "youtube":
        return await validate_youtube_link(raw_item)
    return await validate_telegram_link(get_userbot_client(), raw_item)


@router.message(AddSourceStates.waiting_for_link)
async def receive_source_link(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    platform = data.get("platform")
    raw = (message.text or "").strip()

    if not raw:
        await message.answer("Please send a valid link as text.")
        return

    if platform == "telegram" and get_userbot_client() is None:
        await message.answer(
            "⚠️ Telegram integration isn't set up yet. Ask the bot owner to finish setup.",
            reply_markup=main_menu_keyboard(),
        )
        await state.clear()
        return

    items = _split_bulk_input(raw)
    if not items:
        await message.answer("Please send a valid link as text.")
        return

    if len(items) > _BULK_MAX_ITEMS:
        await message.answer(f"That's {len(items)} links — please send at most {_BULK_MAX_ITEMS} at a time.")
        return

    if len(items) == 1:
        await _add_single_source(message, state, platform, items[0])
    else:
        await _add_multiple_sources(message, state, platform, items)


async def _add_single_source(message: Message, state: FSMContext, platform: str, raw_link: str) -> None:
    status_message = await message.answer("🔎 Validating link...")

    try:
        validated = await _validate_for_platform(platform, raw_link)
    except Exception:
        logger.exception("Unexpected error validating link '%s' for platform %s", raw_link, platform)
        await safe_edit_text(
            status_message,
            "⚠️ Something went wrong while validating the link. Please try again.",
        )
        return

    if not validated:
        if platform == "reddit":
            example = "https://www.reddit.com/r/CryptoCurrency/"
        elif platform == "youtube":
            example = "https://www.youtube.com/@MrBeast"
        else:
            example = "@CoinDesk"
        await safe_edit_text(
            status_message,
            "❌ Couldn't find that source. Double-check the link and try again, "
            "or press Cancel to go back.\n\n"
            f"Example:\n<code>{example}</code>",
            reply_markup=cancel_keyboard(f"platform:{platform}"),
        )
        return

    try:
        user_id = await get_or_create_user(message.from_user.id, message.from_user.username)
        source_id = await get_or_create_source(
            validated.platform, validated.external_id, validated.title, validated.url
        )
        added = await add_subscription(user_id, source_id)
    except Exception:
        logger.exception("Failed to save subscription for user %s", message.from_user.id)
        await safe_edit_text(
            status_message,
            "⚠️ Something went wrong while saving your subscription. Please try again.",
        )
        return

    verb = "✅ Subscribed to" if added else "ℹ️ You're already subscribed to"
    header = f"{verb} <b>{html.escape(validated.title, quote=False)}</b>!"
    await state.clear()
    try:
        list_text, keyboard = await _render_platform_view(message.from_user.id, message.from_user.username, platform)
        await safe_edit_text(status_message, f"{header}\n\n{list_text}", reply_markup=keyboard)
    except Exception:
        logger.exception("Failed to render platform view after adding subscription for user %s", message.from_user.id)
        await safe_edit_text(status_message, header, reply_markup=main_menu_keyboard())


async def _add_multiple_sources(message: Message, state: FSMContext, platform: str, items: list[str]) -> None:
    status_message = await message.answer(f"🔎 Checking {len(items)} link(s), this may take a bit...")

    try:
        user_id = await get_or_create_user(message.from_user.id, message.from_user.username)
    except Exception:
        logger.exception("Failed to register user %s during bulk add", message.from_user.id)
        await safe_edit_text(status_message, "⚠️ Something went wrong. Please try again.")
        return

    added: list[str] = []
    already: list[str] = []
    failed: list[str] = []

    for item in items:
        try:
            validated = await _validate_for_platform(platform, item)
        except Exception:
            logger.exception("Unexpected error validating bulk item '%s'", item)
            validated = None

        if not validated:
            failed.append(item)
            await asyncio.sleep(_BULK_ITEM_DELAY_SECONDS)
            continue

        try:
            source_id = await get_or_create_source(
                validated.platform, validated.external_id, validated.title, validated.url
            )
            was_added = await add_subscription(user_id, source_id)
        except Exception:
            logger.exception("Failed to save bulk subscription for '%s'", item)
            failed.append(item)
            await asyncio.sleep(_BULK_ITEM_DELAY_SECONDS)
            continue

        if was_added:
            added.append(validated.title or validated.url)
        else:
            already.append(validated.title or validated.url)

        await asyncio.sleep(_BULK_ITEM_DELAY_SECONDS)

    lines = [
        f"✅ Added: {len(added)}",
        f"ℹ️ Already subscribed: {len(already)}",
        f"❌ Not found: {len(failed)}",
    ]
    if added:
        lines.append("\n<b>Added:</b>\n" + "\n".join(f"• {html.escape(t, quote=False)}" for t in added))
    if failed:
        lines.append("\n<b>Couldn't find:</b>\n" + "\n".join(f"• {html.escape(t, quote=False)}" for t in failed))
    header = "\n".join(lines)

    await state.clear()
    try:
        list_text, keyboard = await _render_platform_view(message.from_user.id, message.from_user.username, platform)
        await safe_edit_text(status_message, f"{header}\n\n{list_text}", reply_markup=keyboard)
    except Exception:
        logger.exception("Failed to render platform view after bulk-adding subscriptions for user %s", message.from_user.id)
        await safe_edit_text(status_message, header, reply_markup=main_menu_keyboard())


@router.callback_query(F.data.startswith("unsubscribe_list:"))
async def unsubscribe_list(callback: CallbackQuery, state: FSMContext) -> None:
    platform = callback.data.split(":")[1]
    await state.clear()
    try:
        user_id = await get_or_create_user(callback.from_user.id, callback.from_user.username)
        subscriptions = await get_user_subscriptions(user_id)
        filtered = [sub for sub in subscriptions if sub["platform"] == platform]
    except Exception:
        logger.exception("Failed to load subscriptions for user %s platform %s", callback.from_user.id, platform)
        await callback.answer("⚠️ Something went wrong.", show_alert=True)
        return

    if not filtered:
        await callback.answer("You have no subscriptions to remove.", show_alert=True)
        return

    platform_label = PLATFORM_LABELS.get(platform, platform)
    await safe_edit_text(
        callback.message,
        f"Which {platform_label} subscription do you want to remove?",
        reply_markup=unsubscribe_items_keyboard(platform, filtered),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("unsubscribe_item:"))
async def unsubscribe_item(callback: CallbackQuery) -> None:
    _, platform, source_id_str = callback.data.split(":")
    source_id = int(source_id_str)

    try:
        user_id = await get_or_create_user(callback.from_user.id, callback.from_user.username)
        subscriptions = await get_user_subscriptions(user_id)
        match = next((sub for sub in subscriptions if sub["source_id"] == source_id), None)

        if match is None:
            await callback.answer("Already removed.", show_alert=True)
        else:
            await delete_subscription(user_id, source_id)
            await callback.answer(f"Removed {match['title'] or match['url']}")
    except Exception:
        logger.exception("Failed to unsubscribe user %s from source %s", callback.from_user.id, source_id)
        await callback.answer("⚠️ Something went wrong.", show_alert=True)
        return

    try:
        subscriptions = await get_user_subscriptions(user_id)
        filtered = [sub for sub in subscriptions if sub["platform"] == platform]
    except Exception:
        logger.exception("Failed to reload subscriptions after removal for user %s", callback.from_user.id)
        return

    if not filtered:
        try:
            text, keyboard = await _render_platform_view(callback.from_user.id, callback.from_user.username, platform)
            await safe_edit_text(callback.message, text, reply_markup=keyboard)
        except Exception:
            logger.exception("Failed to render platform view after removing last subscription for user %s", callback.from_user.id)
        return

    platform_label = PLATFORM_LABELS.get(platform, platform)
    await safe_edit_text(
        callback.message,
        f"Which {platform_label} subscription do you want to remove?",
        reply_markup=unsubscribe_items_keyboard(platform, filtered),
    )


@router.callback_query(F.data.startswith("unsubscribe_all_prompt:"))
async def unsubscribe_all_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    platform = callback.data.split(":")[1]
    platform_label = PLATFORM_LABELS.get(platform, platform)
    await safe_edit_text(
        callback.message,
        f"⚠️ This will remove ALL your {platform_label} subscriptions. This can't be undone. Are you sure?",
        reply_markup=confirm_unsubscribe_all_keyboard(platform),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("unsubscribe_all_confirm:"))
async def unsubscribe_all_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    platform = callback.data.split(":")[1]
    try:
        user_id = await get_or_create_user(callback.from_user.id, callback.from_user.username)
        removed = await delete_all_subscriptions(user_id, platform)
    except Exception:
        logger.exception("Failed to unsubscribe user %s from all %s sources", callback.from_user.id, platform)
        await callback.answer("⚠️ Something went wrong.", show_alert=True)
        return

    await callback.answer(f"Removed {removed} subscription(s).")
    try:
        text, keyboard = await _render_platform_view(callback.from_user.id, callback.from_user.username, platform)
        await safe_edit_text(callback.message, text, reply_markup=keyboard)
    except Exception:
        logger.exception("Failed to re-render subscriptions after unsubscribe-all for user %s", callback.from_user.id)
