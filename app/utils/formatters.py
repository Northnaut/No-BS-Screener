PLATFORM_LABELS = {
    "reddit": "Reddit",
    "youtube": "YouTube",
}


def format_subscriptions_list(subscriptions: list[dict], platform: str) -> str:
    platform_label = PLATFORM_LABELS.get(platform, platform)
    if not subscriptions:
        return f"📋 <b>{platform_label} subscriptions</b>\n\nNo subscriptions yet. Add one from the menu below."

    lines = [f"📋 <b>{platform_label} subscriptions</b>", ""]
    for sub in subscriptions:
        title = sub["title"] or sub["url"]
        lines.append(f"<b>{title}</b>\n<code>{sub['url']}</code>")
    return "\n\n".join(lines)


def format_alert(source_label: str, title: str, summary: str, url: str) -> str:
    return (
        f"🚨 <b>Important update</b> ({source_label})\n\n"
        f"<b>{title}</b>\n\n"
        f"{summary}\n\n"
        f"🔗 {url}"
    )


def format_video_alert(source_label: str, title: str, url: str) -> str:
    return (
        f"🎬 <b>New video</b> ({source_label})\n\n"
        f"<b>{title}</b>\n\n"
        f"🔗 {url}"
    )
