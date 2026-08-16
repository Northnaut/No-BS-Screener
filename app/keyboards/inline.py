from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


def main_menu_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="👽 Add Reddit", callback_data="add:reddit")
    builder.button(text="▶️ Add YouTube", callback_data="add:youtube")
    builder.button(text="✈️ Add Telegram", callback_data="add:telegram")
    builder.button(text="🎨 Styles", callback_data="menu:styles")
    builder.button(text="📋 My Subscriptions", callback_data="menu:subscriptions")
    builder.adjust(2, 1, 1, 1)
    return builder.as_markup()


def subscriptions_platform_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="👽 Reddit subscriptions", callback_data="subs_tab:reddit")
    builder.button(text="▶️ YouTube subscriptions", callback_data="subs_tab:youtube")
    builder.button(text="✈️ Telegram subscriptions", callback_data="subs_tab:telegram")
    builder.button(text="⬅️ Back", callback_data="menu:main")
    builder.adjust(1)
    return builder.as_markup()


def subscriptions_keyboard(has_subscriptions: bool, active_platform: str, shorts_enabled: bool = True) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if has_subscriptions:
        builder.row(InlineKeyboardButton(text="🗑 Unsubscribe", callback_data=f"unsubscribe_prompt:{active_platform}"))
        builder.row(InlineKeyboardButton(text="🗑 Unsubscribe from ALL", callback_data=f"unsubscribe_all_prompt:{active_platform}"))
    if active_platform == "youtube":
        shorts_text = "🎬 YouTube Shorts: ON" if shorts_enabled else "🎬 YouTube Shorts: OFF"
        builder.row(InlineKeyboardButton(text=shorts_text, callback_data="toggle_shorts"))
    builder.row(InlineKeyboardButton(text="📋 Back to subscriptions menu", callback_data="menu:subscriptions"))
    return builder.as_markup()


def confirm_unsubscribe_all_keyboard(platform: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="⚠️ Yes, unsubscribe from all", callback_data=f"unsubscribe_all_confirm:{platform}"))
    builder.row(InlineKeyboardButton(text="✖️ Cancel", callback_data=f"subs_tab:{platform}"))
    return builder.as_markup()


def styles_keyboard(current_style: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    options = [
        ("original", "📰 Original"),
        ("brief", "⚡ TL;DR"),
        ("degen", "💬 Plain English"),
    ]
    for key, label in options:
        text = f"✅ {label}" if key == current_style else label
        builder.row(InlineKeyboardButton(text=text, callback_data=f"set_style:{key}"))
    builder.row(InlineKeyboardButton(text="⬅️ Back", callback_data="menu:main"))
    return builder.as_markup()


def cancel_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✖️ Cancel", callback_data="menu:main")
    return builder.as_markup()
