import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.database.queries import (
    add_subscription,
    delete_subscription,
    get_or_create_source,
    get_or_create_user,
    get_user_subscriptions,
    get_youtube_shorts_enabled,
    toggle_youtube_shorts,
)
from app.keyboards.inline import (
    cancel_keyboard,
    main_menu_keyboard,
    subscriptions_keyboard,
    subscriptions_platform_keyboard,
)
from app.parsers.validators import extract_external_id, validate_reddit_link, validate_youtube_link
from app.states.add_source import AddSourceStates
from app.utils.formatters import format_subscriptions_list
from app.utils.telegram import safe_edit_text

logger = logging.getLogger(__name__)
router = Router()


@router.callback_query(F.data == "menu:main")
async def show_main_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await safe_edit_text(callback.message, "Main menu:", reply_markup=main_menu_keyboard())
    await callback.answer()


@router.callback_query(F.data == "toggle_shorts")
async def toggle_shorts_handler(callback: CallbackQuery) -> None:
    new_state = await toggle_youtube_shorts(callback.from_user.id)
    try:
        await _send_subscriptions_tab(callback.message, callback.from_user.id, callback.from_user.username, "youtube")
    except Exception:
        logger.exception("Failed to re-render subscriptions after toggling shorts for user %s", callback.from_user.id)
        await callback.answer("⚠️ Failed to update.", show_alert=True)
        return
    await callback.answer(f"YouTube Shorts: {'ON' if new_state else 'OFF'}")


@router.callback_query(F.data == "noop")
async def noop_callback(callback: CallbackQuery) -> None:
    await callback.answer()


@router.callback_query(F.data.in_({"add:reddit", "add:youtube"}))
async def start_add_source(callback: CallbackQuery, state: FSMContext) -> None:
    platform = callback.data.split(":")[1]
    await state.update_data(platform=platform)
    await state.set_state(AddSourceStates.waiting_for_link)

    if platform == "reddit":
        prompt = (
            "Send me a Reddit link.\n\n"
            "Example:\n<code>https://www.reddit.com/r/CryptoCurrency/</code>"
        )
    else:
        prompt = (
            "Send me a YouTube channel link.\n\n"
            "Example:\n<code>https://www.youtube.com/@MrBeast</code>"
        )

    await safe_edit_text(
        callback.message,
        prompt,
        reply_markup=cancel_keyboard(),
    )
    await callback.answer()


@router.message(AddSourceStates.waiting_for_link)
async def receive_source_link(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    platform = data.get("platform")
    raw_link = (message.text or "").strip()

    if not raw_link:
        await message.answer("Please send a valid link as text.")
        return

    status_message = await message.answer("🔎 Validating link...")

    try:
        if platform == "reddit":
            validated = await validate_reddit_link(raw_link)
        else:
            validated = await validate_youtube_link(raw_link)
    except Exception:
        logger.exception("Unexpected error validating link '%s' for platform %s", raw_link, platform)
        await safe_edit_text(
            status_message,
            "⚠️ Something went wrong while validating the link. Please try again.",
        )
        return

    if not validated:
        example = (
            "https://www.reddit.com/r/CryptoCurrency/"
            if platform == "reddit"
            else "https://www.youtube.com/@MrBeast"
        )
        await safe_edit_text(
            status_message,
            "❌ Couldn't find that source. Double-check the link and try again, "
            "or press Cancel to go back.\n\n"
            f"Example:\n<code>{example}</code>",
            reply_markup=cancel_keyboard(),
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

    await state.clear()

    if added:
        await safe_edit_text(
            status_message,
            f"✅ Subscribed to <b>{validated.title}</b>!",
            reply_markup=main_menu_keyboard(),
        )
    else:
        await safe_edit_text(
            status_message,
            f"ℹ️ You're already subscribed to <b>{validated.title}</b>.",
            reply_markup=main_menu_keyboard(),
        )


async def _send_subscriptions_tab(message: Message, user_tg_id: int, username: str | None, active_platform: str) -> None:
    user_id = await get_or_create_user(user_tg_id, username)
    subscriptions = await get_user_subscriptions(user_id)
    filtered = [sub for sub in subscriptions if sub["platform"] == active_platform]

    shorts_enabled = True
    if active_platform == "youtube":
        shorts_enabled = await get_youtube_shorts_enabled(user_tg_id)

    text = format_subscriptions_list(filtered, active_platform)
    await message.answer(text, reply_markup=subscriptions_keyboard(bool(filtered), active_platform, shorts_enabled))


@router.callback_query(F.data == "menu:subscriptions")
async def show_subscriptions(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await safe_edit_text(callback.message, "Choose a platform:", reply_markup=subscriptions_platform_keyboard())
    await callback.answer()


@router.callback_query(F.data.startswith("subs_tab:"))
async def switch_subscriptions_tab(callback: CallbackQuery, state: FSMContext) -> None:
    platform = callback.data.split(":")[1]
    await state.clear()
    await state.update_data(subs_tab=platform)

    try:
        await _send_subscriptions_tab(callback.message, callback.from_user.id, callback.from_user.username, platform)
    except Exception:
        logger.exception("Failed to load subscriptions for user %s", callback.from_user.id)
        await callback.answer("⚠️ Failed to load subscriptions.", show_alert=True)
        return

    await callback.answer()


@router.callback_query(F.data.startswith("unsubscribe_prompt:"))
async def unsubscribe_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    platform = callback.data.split(":")[1]
    await state.update_data(subs_tab=platform, unsubscribe_platform=platform)
    await state.set_state(AddSourceStates.waiting_for_unsubscribe_link)

    platform_label = "Reddit" if platform == "reddit" else "YouTube"
    await safe_edit_text(
        callback.message,
        f"Send me the {platform_label} link you want to unsubscribe from "
        "(copy it from the list above).",
        reply_markup=cancel_keyboard(),
    )
    await callback.answer()


@router.message(AddSourceStates.waiting_for_unsubscribe_link)
async def receive_unsubscribe_link(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    platform = data.get("unsubscribe_platform", "reddit")
    raw_link = (message.text or "").strip()

    if not raw_link:
        await message.answer("Please send a valid link as text.")
        return

    external_id = extract_external_id(platform, raw_link)

    if not external_id and platform == "youtube":
        validated = await validate_youtube_link(raw_link)
        external_id = validated.external_id if validated else None

    if not external_id:
        await message.answer(
            "❌ Couldn't recognize that link. Please copy it exactly from the list above, "
            "or press Cancel to go back.",
            reply_markup=cancel_keyboard(),
        )
        return

    try:
        user_id = await get_or_create_user(message.from_user.id, message.from_user.username)
        subscriptions = await get_user_subscriptions(user_id)
        match = next(
            (sub for sub in subscriptions if sub["platform"] == platform and sub["external_id"] == external_id),
            None,
        )

        if not match:
            await message.answer(
                "❌ You're not subscribed to that source. Please copy the link exactly from the list above, "
                "or press Cancel to go back.",
                reply_markup=cancel_keyboard(),
            )
            return

        await delete_subscription(user_id, match["source_id"])
    except Exception:
        logger.exception("Failed to unsubscribe user %s from platform %s link '%s'", message.from_user.id, platform, raw_link)
        await message.answer("⚠️ Something went wrong while removing your subscription. Please try again.")
        return

    await state.clear()
    await message.answer(f"✅ Unsubscribed from <b>{match['title'] or match['url']}</b>.")
    await _send_subscriptions_tab(message, message.from_user.id, message.from_user.username, platform)
