import html

from app.parsers.newspapers import CATEGORY_LABELS, NEWSPAPER_FEEDS

PLATFORM_LABELS = {
    "reddit": "Reddit",
    "youtube": "YouTube",
    "telegram": "Telegram",
}


def _esc(value: str) -> str:
    """Escape text going into HTML *content* (between tags). Telegram's HTML parser
    doesn't reliably decode &#x27;/&quot; back to '/" in plain text, so quotes must
    NOT be escaped here — only & < > need it. Use _esc_attr for attribute values."""
    return html.escape(value, quote=False)


def _esc_attr(value: str) -> str:
    """Escape text going into an HTML *attribute* value (e.g. href="..."), where a
    literal quote character would break out of the attribute."""
    return html.escape(value, quote=True)


def format_subscriptions_list(subscriptions: list[dict], platform: str) -> str:
    platform_label = PLATFORM_LABELS.get(platform, platform)
    if not subscriptions:
        return f"📋 <b>{platform_label} subscriptions</b>\n\nNo subscriptions yet. Add one from the menu below."

    lines = [f"📋 <b>{platform_label} subscriptions</b>"]
    for sub in subscriptions:
        title = _esc(sub["title"] or sub["url"])
        url_attr = _esc_attr(sub["url"])
        lines.append(f'<a href="{url_attr}"><b>{title}</b></a>')
    return "\n\n".join(lines)


_MAX_ORIGINAL_CHARS = 600
_MAX_SUMMARY_CHARS = 220
_MAX_TITLE_CHARS = 300
_MAX_LABEL_CHARS = 150


def _truncate(value: str, max_len: int) -> str:
    value = (value or "").strip()
    if len(value) > max_len:
        return value[:max_len] + "…"
    return value


STYLE_LABELS = {
    "original": "📰 Original",
    "brief": "⚡ TL;DR",
    "degen": "💬 Casual",
    "eli5": "🧒 ELI5",
    "tiktok": "🎵 TikTok",
}


def _alert_body_lines(style: str, title: str, original_text: str, summaries: dict[str, str]) -> list[str]:
    title_raw = (title or "").strip()
    original_raw = (original_text or "").strip()
    title = _truncate(title, _MAX_TITLE_CHARS)
    original = _truncate(original_text, _MAX_ORIGINAL_CHARS)

    if style == "original":
        # The literal, unprocessed post. Some sources (Telegram) derive the title
        # from the post's own first line, so title == the start of original_text —
        # showing both would repeat the same sentence twice.
        title_is_duplicated = bool(original_raw) and original_raw.startswith(title_raw)
        parts = []
        if title and not title_is_duplicated:
            parts.append(f"<b>{_esc(title)}</b>")
        if original:
            parts.append(_esc(original))
        return parts

    # The AI-written line IS the headline, not a raw title plus a summary underneath —
    # that duplication is what made every style look like "original". If the requested
    # style's text is missing (AI failure), fall back to brief, then any other style
    # that came back non-empty, then the raw original as a last resort.
    body = _truncate(summaries.get(style, ""), _MAX_SUMMARY_CHARS)
    if not body:
        body = _truncate(summaries.get("brief", ""), _MAX_SUMMARY_CHARS)
    if not body:
        body = next((_truncate(v, _MAX_SUMMARY_CHARS) for v in summaries.values() if v), "")
    if not body:
        body = original
    return [f"<b>{_esc(body)}</b>"] if body else []


def format_alert(style: str, source_label: str, title: str, original_text: str, summaries: dict[str, str], url: str) -> str:
    source_label = _truncate(source_label, _MAX_LABEL_CHARS)
    header = f"🚨 <b>Important update</b> ({_esc(source_label)})"
    learn_more = f'🔗 <a href="{_esc_attr(url)}">Learn more</a>'

    parts = [header, *_alert_body_lines(style, title, original_text, summaries), learn_more]
    return "\n\n".join(parts)


def format_newspaper_alert(
    style: str, source_label: str, title: str, original_text: str, summaries: dict[str, str], url: str
) -> str:
    source_label = _truncate(source_label, _MAX_LABEL_CHARS)
    header = f"📰 <b>News</b> ({_esc(source_label)})"
    learn_more = f'🔗 <a href="{_esc_attr(url)}">Learn more</a>'

    parts = [header, *_alert_body_lines(style, title, original_text, summaries), learn_more]
    return "\n\n".join(parts)


def format_newspaper_sources() -> str:
    by_category: dict[str, list[tuple[str, str]]] = {}
    for title, category, _rss_url, homepage in NEWSPAPER_FEEDS:
        by_category.setdefault(category, []).append((title, homepage))

    lines = ["📚 <b>News Sources</b>", ""]
    for category, label in CATEGORY_LABELS.items():
        sources = by_category.get(category, [])
        if not sources:
            continue
        lines.append(f"<b>{_esc(label)}</b>")
        for title, homepage in sources:
            lines.append(f'<a href="{_esc_attr(homepage)}">{_esc(title)}</a>')
        lines.append("")

    return "\n".join(lines).rstrip()


def format_video_alert(source_label: str, title: str, url: str) -> str:
    return (
        f"🎬 <b>New video</b> ({_esc(source_label)})\n\n"
        f"<b>{_esc(title)}</b>\n\n"
        f"🔗 {_esc(url)}"
    )
