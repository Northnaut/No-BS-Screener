import logging
from datetime import datetime, timezone
from typing import Optional

from app.database.connection import get_connection

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def get_or_create_user(tg_id: int, username: Optional[str]) -> int:
    async with get_connection() as conn:
        await conn.execute(
            "INSERT INTO users (tg_id, username, is_active, created_at) VALUES (?, ?, 1, ?) "
            "ON CONFLICT(tg_id) DO UPDATE SET username = excluded.username, is_active = 1",
            (tg_id, username, _now()),
        )
        cursor = await conn.execute("SELECT id FROM users WHERE tg_id = ?", (tg_id,))
        row = await cursor.fetchone()
        return row["id"]


async def deactivate_user(tg_id: int) -> None:
    async with get_connection() as conn:
        await conn.execute("UPDATE users SET is_active = 0 WHERE tg_id = ?", (tg_id,))
    logger.info("Deactivated user tg_id=%s (bot blocked)", tg_id)


async def get_or_create_source(platform: str, external_id: str, title: Optional[str], url: str) -> int:
    async with get_connection() as conn:
        await conn.execute(
            """
            INSERT INTO sources (platform, external_id, title, url, is_bootstrapped, fail_count, created_at)
            VALUES (?, ?, ?, ?, 0, 0, ?)
            ON CONFLICT(platform, external_id) DO NOTHING
            """,
            (platform, external_id, title, url, _now()),
        )
        cursor = await conn.execute(
            "SELECT id FROM sources WHERE platform = ? AND external_id = ?",
            (platform, external_id),
        )
        row = await cursor.fetchone()
        return row["id"]


async def add_subscription(user_id: int, source_id: int) -> bool:
    async with get_connection() as conn:
        cursor = await conn.execute(
            "INSERT OR IGNORE INTO subscriptions (user_id, source_id, created_at) VALUES (?, ?, ?)",
            (user_id, source_id, _now()),
        )
        return cursor.rowcount > 0


async def get_user_subscriptions(user_id: int) -> list[dict]:
    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            SELECT s.id AS source_id, s.platform, s.external_id, s.title, s.url
            FROM subscriptions sub
            JOIN sources s ON s.id = sub.source_id
            WHERE sub.user_id = ?
            ORDER BY sub.created_at DESC
            """,
            (user_id,),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def delete_subscription(user_id: int, source_id: int) -> None:
    async with get_connection() as conn:
        await conn.execute(
            "DELETE FROM subscriptions WHERE user_id = ? AND source_id = ?",
            (user_id, source_id),
        )


async def get_all_active_sources() -> list[dict]:
    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            SELECT DISTINCT s.*
            FROM sources s
            JOIN subscriptions sub ON sub.source_id = s.id
            JOIN users u ON u.id = sub.user_id
            WHERE u.is_active = 1
            """
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def get_source_subscribers(source_id: int, is_short: bool = False) -> list[dict]:
    async with get_connection() as conn:
        query = """
            SELECT u.id, u.tg_id
            FROM subscriptions sub
            JOIN users u ON u.id = sub.user_id
            WHERE sub.source_id = ? AND u.is_active = 1
        """
        if is_short:
            query += " AND u.youtube_shorts_enabled = 1"
        cursor = await conn.execute(query, (source_id,))
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def get_youtube_shorts_enabled(tg_id: int) -> bool:
    async with get_connection() as conn:
        cursor = await conn.execute("SELECT youtube_shorts_enabled FROM users WHERE tg_id = ?", (tg_id,))
        row = await cursor.fetchone()
        return bool(row["youtube_shorts_enabled"]) if row else True


async def toggle_youtube_shorts(tg_id: int) -> bool:
    async with get_connection() as conn:
        await conn.execute(
            "UPDATE users SET youtube_shorts_enabled = 1 - youtube_shorts_enabled WHERE tg_id = ?",
            (tg_id,),
        )
        cursor = await conn.execute("SELECT youtube_shorts_enabled FROM users WHERE tg_id = ?", (tg_id,))
        row = await cursor.fetchone()
        return bool(row["youtube_shorts_enabled"])


async def is_post_seen(source_id: int, post_external_id: str) -> bool:
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT id FROM seen_posts WHERE source_id = ? AND post_external_id = ?",
            (source_id, post_external_id),
        )
        return await cursor.fetchone() is not None


async def claim_post_if_unseen(source_id: int, post_external_id: str, title: str, text: str, url: str) -> bool:
    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            INSERT OR IGNORE INTO seen_posts (source_id, post_external_id, title, text, url, is_important, summary, created_at)
            VALUES (?, ?, ?, ?, ?, NULL, NULL, ?)
            """,
            (source_id, post_external_id, title, text, url, _now()),
        )
        return cursor.rowcount > 0


async def get_next_unclassified_post() -> Optional[dict]:
    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            SELECT sp.source_id, sp.post_external_id, sp.title, sp.text, sp.url,
                   s.platform, s.title AS source_title
            FROM seen_posts sp
            JOIN sources s ON s.id = sp.source_id
            WHERE sp.is_important IS NULL
            ORDER BY sp.created_at ASC
            LIMIT 1
            """
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def save_seen_post(
    source_id: int,
    post_external_id: str,
    title: str,
    url: str,
    is_important: Optional[bool] = None,
    summary: Optional[str] = None,
) -> None:
    async with get_connection() as conn:
        await conn.execute(
            """
            INSERT INTO seen_posts (source_id, post_external_id, title, url, is_important, summary, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_id, post_external_id) DO UPDATE SET
                is_important = excluded.is_important,
                summary = excluded.summary,
                text = ''
            """,
            (
                source_id,
                post_external_id,
                title,
                url,
                None if is_important is None else int(is_important),
                summary,
                _now(),
            ),
        )


async def mark_source_checked(source_id: int, success: bool) -> None:
    async with get_connection() as conn:
        if success:
            await conn.execute(
                """
                UPDATE sources
                SET last_checked_at = ?, is_bootstrapped = 1, fail_count = 0
                WHERE id = ?
                """,
                (_now(), source_id),
            )
        else:
            await conn.execute(
                "UPDATE sources SET fail_count = fail_count + 1 WHERE id = ?",
                (source_id,),
            )


async def purge_old_seen_posts(older_than_days: int = 30) -> int:
    async with get_connection() as conn:
        cursor = await conn.execute(
            "DELETE FROM seen_posts WHERE created_at < datetime('now', ?)",
            (f"-{older_than_days} days",),
        )
        return cursor.rowcount
