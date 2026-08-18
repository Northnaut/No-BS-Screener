from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.parsers.newspapers import CATEGORY_LABELS

_PLATFORM_ADD_LABELS = {
    "reddit": "➕ Add Reddit",
    "youtube": "➕ Add YouTube",
    "telegram": "➕ Add Telegram",
}

_UNSUBSCRIBE_BUTTON_MAX_CHARS = 60


def main_menu_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="👽 Reddit", callback_data="platform:reddit")
    builder.button(text="▶️ YouTube", callback_data="platform:youtube")
    builder.button(text="✈️ Telegram", callback_data="platform:telegram")
    builder.button(text="📰 Newspapers", callback_data="menu:newspapers")
    builder.button(text="🎨 Styles", callback_data="menu:styles")
    builder.adjust(3, 1, 1)
    return builder.as_markup()


def platform_keyboard(platform: str, has_subscriptions: bool, shorts_enabled: bool = True) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text=_PLATFORM_ADD_LABELS.get(platform, "➕ Add"), callback_data=f"add:{platform}"))
    if has_subscriptions:
        builder.row(InlineKeyboardButton(text="🗑 Unsubscribe", callback_data=f"unsubscribe_list:{platform}"))
        builder.row(InlineKeyboardButton(text="🗑 Unsubscribe from ALL", callback_data=f"unsubscribe_all_prompt:{platform}"))
    if platform == "youtube":
        shorts_text = "🎬 YouTube Shorts: ON" if shorts_enabled else "🎬 YouTube Shorts: OFF"
        builder.row(InlineKeyboardButton(text=shorts_text, callback_data="toggle_shorts"))
    builder.row(InlineKeyboardButton(text="⬅️ Back", callback_data="menu:main"))
    return builder.as_markup()


def unsubscribe_items_keyboard(platform: str, subscriptions: list[dict]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for sub in subscriptions:
        label = sub["title"] or sub["url"]
        if len(label) > _UNSUBSCRIBE_BUTTON_MAX_CHARS:
            label = label[:_UNSUBSCRIBE_BUTTON_MAX_CHARS] + "…"
        builder.row(InlineKeyboardButton(text=f"❌ {label}", callback_data=f"unsubscribe_item:{platform}:{sub['source_id']}"))
    builder.row(InlineKeyboardButton(text="⬅️ Back", callback_data=f"platform:{platform}"))
    return builder.as_markup()


def confirm_unsubscribe_all_keyboard(platform: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="⚠️ Yes, unsubscribe from all", callback_data=f"unsubscribe_all_confirm:{platform}"))
    builder.row(InlineKeyboardButton(text="✖️ Cancel", callback_data=f"platform:{platform}"))
    return builder.as_markup()


def newspapers_keyboard(enabled_categories: list[str]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="📚 Sources", callback_data="newspaper_sources"))
    for key, label in CATEGORY_LABELS.items():
        text = f"✅ {label}" if key in enabled_categories else label
        builder.row(InlineKeyboardButton(text=text, callback_data=f"toggle_newspaper_cat:{key}"))
    builder.row(InlineKeyboardButton(text="⬅️ Back", callback_data="menu:main"))
    return builder.as_markup()


def newspaper_sources_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="⬅️ Back", callback_data="menu:newspapers"))
    return builder.as_markup()


def styles_keyboard(current_style: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    options = [
        ("original", "📰 Original"),
        ("brief", "⚡ TL;DR"),
        ("degen", "💬 Casual"),
        ("eli5", "🧒 ELI5"),
        ("tiktok", "🎵 TikTok"),
    ]
    for key, label in options:
        text = f"✅ {label}" if key == current_style else label
        builder.row(InlineKeyboardButton(text=text, callback_data=f"set_style:{key}"))
    builder.row(InlineKeyboardButton(text="⬅️ Back", callback_data="menu:main"))
    return builder.as_markup()


def cancel_keyboard(back_to: str = "menu:main") -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✖️ Cancel", callback_data=back_to)
    return builder.as_markup()
