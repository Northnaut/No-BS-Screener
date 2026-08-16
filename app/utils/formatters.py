import html

PLATFORM_LABELS = {
    "reddit": "Reddit",
    "youtube": "YouTube",
    "telegram": "Telegram",
}


def _esc(value: str) -> str:
    return html.escape(value, quote=True)


def format_subscriptions_list(subscriptions: list[dict], platform: str) -> str:
    platform_label = PLATFORM_LABELS.get(platform, platform)
    if not subscriptions:
        return f"📋 <b>{platform_label} subscriptions</b>\n\nNo subscriptions yet. Add one from the menu below."

    lines = [f"📋 <b>{platform_label} subscriptions</b>", ""]
    for sub in subscriptions:
        title = _esc(sub["title"] or sub["url"])
        url = _esc(sub["url"])
        lines.append(f'<a href="{url}"><b>{title}</b></a>\n<code>{url}</code>')
    return "\n\n".join(lines)


_MAX_ORIGINAL_CHARS = 600
_MAX_SUMMARY_CHARS = 500
_MAX_TITLE_CHARS = 300
_MAX_LABEL_CHARS = 150


def _truncate(value: str, max_len: int) -> str:
    value = (value or "").strip()
    if len(value) > max_len:
        return value[:max_len] + "…"
    return value


def format_alert(source_label: str, title: str, original_text: str, summary_brief: str, summary_degen: str, url: str) -> str:
    source_label = _truncate(source_label, _MAX_LABEL_CHARS)
    title = _truncate(title, _MAX_TITLE_CHARS)
    original = _truncate(original_text, _MAX_ORIGINAL_CHARS)
    summary_brief = _truncate(summary_brief, _MAX_SUMMARY_CHARS)
    summary_degen = _truncate(summary_degen, _MAX_SUMMARY_CHARS)

    parts = [
        f"🚨 <b>Important update</b> ({_esc(source_label)})",
        f"<b>{_esc(title)}</b>",
    ]
    if original:
        parts.append(f"📰 <b>Original:</b>\n{_esc(original)}")
    if summary_brief:
        parts.append(f"⚡ <b>TL;DR:</b> {_esc(summary_brief)}")
    if summary_degen:
        parts.append(f"💬 <b>Plain English:</b> {_esc(summary_degen)}")
    parts.append(f"🔗 {_esc(url)}")

    return "\n\n".join(parts)


def format_video_alert(source_label: str, title: str, url: str) -> str:
    return (
        f"🎬 <b>New video</b> ({_esc(source_label)})\n\n"
        f"<b>{_esc(title)}</b>\n\n"
        f"🔗 {_esc(url)}"
    )
