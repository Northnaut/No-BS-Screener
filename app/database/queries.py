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


async def delete_all_subscriptions(user_id: int, platform: str) -> int:
    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            DELETE FROM subscriptions
            WHERE user_id = ? AND source_id IN (SELECT id FROM sources WHERE platform = ?)
            """,
            (user_id, platform),
        )
        return cursor.rowcount


async def upsert_newspaper_source(external_id: str, title: str, url: str, category: str) -> int:
    """Idempotent insert-or-update, safe to call on every startup. Preserves is_bootstrapped/
    fail_count across restarts (only title/url/category are re-synced from the code-defined list)."""
    async with get_connection() as conn:
        await conn.execute(
            """
            INSERT INTO sources (platform, external_id, title, url, category, is_bootstrapped, fail_count, created_at)
            VALUES ('newspaper', ?, ?, ?, ?, 0, 0, ?)
            ON CONFLICT(platform, external_id) DO UPDATE SET
                title = excluded.title, url = excluded.url, category = excluded.category
            """,
            (external_id, title, url, category, _now()),
        )
        cursor = await conn.execute(
            "SELECT id FROM sources WHERE platform = 'newspaper' AND external_id = ?",
            (external_id,),
        )
        row = await cursor.fetchone()
        return row["id"]


async def delete_stale_newspaper_sources(keep_external_ids: list[str]) -> int:
    async with get_connection() as conn:
        placeholders = ",".join("?" for _ in keep_external_ids)
        cursor = await conn.execute(
            f"DELETE FROM sources WHERE platform = 'newspaper' AND external_id NOT IN ({placeholders})",
            keep_external_ids,
        )
        return cursor.rowcount


async def get_newspaper_sources(limit: Optional[int] = None) -> list[dict]:
    """Returns the least-recently-checked feeds first. The caller used to fetch all ~38 rows
    and random.sample() a handful, which samples with replacement across cycles — coverage
    was geometric, so feeds could go hours unpolled (one was measured 1h50m stale against a
    46min mean) and fast feeds silently rotated items out of their RSS window before being
    read. Ordering by last_checked_at makes the rotation deterministic: every feed gets its
    turn exactly once per full cycle, with never-checked feeds first."""
    async with get_connection() as conn:
        sql = (
            "SELECT * FROM sources WHERE platform = 'newspaper' "
            "ORDER BY last_checked_at IS NOT NULL, last_checked_at ASC"
        )
        params: tuple = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (limit,)
        cursor = await conn.execute(sql, params)
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def get_newspaper_categories(user_id: int) -> list[str]:
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT category FROM newspaper_category_subs WHERE user_id = ?", (user_id,)
        )
        rows = await cursor.fetchall()
        return [row["category"] for row in rows]


async def toggle_newspaper_category(user_id: int, category: str) -> bool:
    """Returns True if the category is now enabled, False if it was just disabled."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "DELETE FROM newspaper_category_subs WHERE user_id = ? AND category = ?",
            (user_id, category),
        )
        if cursor.rowcount > 0:
            # Drop whatever's still waiting in this user's delivery queue for the
            # category they just left — no point holding onto posts they'll never see.
            await conn.execute(
                "DELETE FROM newspaper_delivery_queue WHERE user_id = ? AND category = ?",
                (user_id, category),
            )
            return False
        await conn.execute(
            "INSERT OR IGNORE INTO newspaper_category_subs (user_id, category, created_at) VALUES (?, ?, ?)",
            (user_id, category, _now()),
        )
        return True


async def get_newspaper_category_subscribers(category: str) -> list[dict]:
    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            SELECT u.id, u.tg_id, u.summary_style
            FROM newspaper_category_subs ncs
            JOIN users u ON u.id = ncs.user_id
            WHERE ncs.category = ? AND u.is_active = 1
            """,
            (category,),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


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
            SELECT u.id, u.tg_id, u.summary_style
            FROM subscriptions sub
            JOIN users u ON u.id = sub.user_id
            WHERE sub.source_id = ? AND u.is_active = 1
        """
        if is_short:
            query += " AND u.youtube_shorts_enabled = 1"
        cursor = await conn.execute(query, (source_id,))
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


_VALID_SUMMARY_STYLES = ("original", "brief", "degen", "eli5", "tiktok")
_DEFAULT_SUMMARY_STYLE = "brief"


async def get_summary_style(tg_id: int) -> str:
    async with get_connection() as conn:
        cursor = await conn.execute("SELECT summary_style FROM users WHERE tg_id = ?", (tg_id,))
        row = await cursor.fetchone()
        if row is None or not row["summary_style"]:
            return _DEFAULT_SUMMARY_STYLE
        return row["summary_style"]


async def set_summary_style(tg_id: int, style: str) -> None:
    async with get_connection() as conn:
        await conn.execute("UPDATE users SET summary_style = ? WHERE tg_id = ?", (style, tg_id))


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


async def claim_unseen_posts(source_id: int, posts: list) -> set:
    """Claims a whole feed's worth of posts over ONE connection and ONE transaction,
    returning the external_ids that were newly claimed.

    The per-post version opened a fresh connection (and paid a WAL checkpoint) for each of
    up to 25 posts per source per cycle, nearly all of which lose on the UNIQUE constraint
    because they were already seen. Note that a losing INSERT OR IGNORE still consumes an
    AUTOINCREMENT value, which is why seen_posts.id had reached ~203,000 for 2,943 live rows.

    The claim stays an atomic INSERT OR IGNORE rather than a check-then-insert, so it remains
    race-free against the concurrent newspaper fan-out."""
    if not posts:
        return set()

    claimed = set()
    now = _now()
    async with get_connection() as conn:
        for post in posts:
            cursor = await conn.execute(
                """
                INSERT OR IGNORE INTO seen_posts (source_id, post_external_id, title, text, url, is_important, summary, created_at)
                VALUES (?, ?, ?, ?, ?, NULL, NULL, ?)
                """,
                (source_id, post.external_id, post.title, post.text, post.url, now),
            )
            if cursor.rowcount > 0:
                claimed.add(post.external_id)
    return claimed


# 38 curated newspaper feeds produce far more unclassified posts than the handful of
# reddit/telegram sources. Newspapers and reddit/telegram are classified with separate
# batch AI calls (different prompts — newspapers skip importance triage entirely), each
# drawing from its own platform group, so neither can starve the other of AI throughput.
async def get_unclassified_posts(platforms: tuple[str, ...], limit: int) -> list[dict]:
    async with get_connection() as conn:
        placeholders = ",".join("?" for _ in platforms)
        cursor = await conn.execute(
            f"""
            SELECT sp.id AS seen_post_id, sp.source_id, sp.post_external_id, sp.title,
                   sp.text, sp.url, s.platform, s.title AS source_title, s.category
            FROM seen_posts sp
            JOIN sources s ON s.id = sp.source_id
            WHERE sp.is_important IS NULL
              AND sp.classification_attempts < ?
              AND s.platform IN ({placeholders})
            ORDER BY sp.created_at ASC
            LIMIT ?
            """,
            (MAX_CLASSIFICATION_ATTEMPTS, *platforms, limit),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


# A batch that fails is deliberately left unclassified so it retries, but without a cap a
# permanently-poisonous batch would sit at the head of the oldest-first queue forever and
# starve everything behind it. After this many failed attempts a post is passed over.
MAX_CLASSIFICATION_ATTEMPTS = 5


async def bump_classification_attempts(seen_post_ids: list[int]) -> None:
    """Records a failed classification attempt for a whole batch in one statement."""
    if not seen_post_ids:
        return
    async with get_connection() as conn:
        placeholders = ",".join("?" for _ in seen_post_ids)
        await conn.execute(
            f"UPDATE seen_posts SET classification_attempts = classification_attempts + 1 "
            f"WHERE id IN ({placeholders})",
            tuple(seen_post_ids),
        )


async def save_seen_post(
    source_id: int,
    post_external_id: str,
    title: str,
    url: str,
    is_important: Optional[bool] = None,
    summary: Optional[str] = None,
    summary_degen: Optional[str] = None,
    summary_eli5: Optional[str] = None,
    summary_tiktok: Optional[str] = None,
    keep_text: bool = False,
) -> int:
    """keep_text=True skips wiping seen_posts.text on classification — needed for
    newspapers, whose delivery is now queued and can happen well after classification
    (the "original" style needs the source text to still be around when it's finally sent).
    Reddit/YouTube/Telegram broadcast immediately off the in-memory post text regardless of
    what's in the DB, so wiping it right after classification is safe for them."""
    async with get_connection() as conn:
        await conn.execute(
            f"""
            INSERT INTO seen_posts (
                source_id, post_external_id, title, url, is_important,
                summary, summary_degen, summary_eli5, summary_tiktok, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_id, post_external_id) DO UPDATE SET
                is_important = excluded.is_important,
                summary = excluded.summary,
                summary_degen = excluded.summary_degen,
                summary_eli5 = excluded.summary_eli5,
                summary_tiktok = excluded.summary_tiktok
                {"" if keep_text else ", text = ''"}
            """,
            (
                source_id,
                post_external_id,
                title,
                url,
                None if is_important is None else int(is_important),
                summary,
                summary_degen,
                summary_eli5,
                summary_tiktok,
                _now(),
            ),
        )
        cursor = await conn.execute(
            "SELECT id FROM seen_posts WHERE source_id = ? AND post_external_id = ?",
            (source_id, post_external_id),
        )
        row = await cursor.fetchone()
        return row["id"]


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


async def enqueue_newspaper_delivery(user_id: int, seen_post_id: int, category: str, max_per_user: int) -> None:
    """Queues a classified newspaper post for personal, rate-limited delivery to one
    subscriber. Trims the user's queue down to max_per_user right after, dropping the
    oldest entries first — a backlog of stale headlines is worth less than fresh ones."""
    async with get_connection() as conn:
        await conn.execute(
            "INSERT OR IGNORE INTO newspaper_delivery_queue (user_id, seen_post_id, category, created_at) VALUES (?, ?, ?, ?)",
            (user_id, seen_post_id, category, _now()),
        )
        await conn.execute(
            """
            DELETE FROM newspaper_delivery_queue
            WHERE user_id = ? AND id NOT IN (
                SELECT id FROM newspaper_delivery_queue WHERE user_id = ? ORDER BY created_at DESC LIMIT ?
            )
            """,
            (user_id, user_id, max_per_user),
        )


async def get_users_due_for_newspaper_alert(interval_minutes: int) -> list[dict]:
    """Active users who have at least one newspaper post waiting and haven't been sent
    one within the last interval_minutes (or never have been sent one at all)."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            SELECT DISTINCT u.id, u.tg_id, u.summary_style
            FROM users u
            JOIN newspaper_delivery_queue q ON q.user_id = u.id
            WHERE u.is_active = 1
              AND (u.last_newspaper_alert_at IS NULL OR datetime(u.last_newspaper_alert_at) <= datetime('now', ?))
            """,
            (f"-{interval_minutes} minutes",),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def delete_newspaper_delivery(queue_id: int) -> None:
    async with get_connection() as conn:
        await conn.execute("DELETE FROM newspaper_delivery_queue WHERE id = ?", (queue_id,))


async def peek_next_newspaper_delivery(user_id: int) -> Optional[dict]:
    """Returns the oldest queued post the user is still subscribed to, WITHOUT removing it —
    the caller deletes it via delete_newspaper_delivery only once the send is confirmed.
    Deleting up front meant any network blip destroyed the item outright. Queue entries for
    categories the user has since unsubscribed from are still discarded here, since those
    should never be sent at all."""
    async with get_connection() as conn:
        while True:
            cursor = await conn.execute(
                """
                SELECT q.id AS queue_id, q.category, sp.title, sp.url, sp.text AS original_text,
                       sp.summary, sp.summary_degen, sp.summary_eli5, sp.summary_tiktok,
                       s.title AS source_title
                FROM newspaper_delivery_queue q
                JOIN seen_posts sp ON sp.id = q.seen_post_id
                JOIN sources s ON s.id = sp.source_id
                WHERE q.user_id = ?
                ORDER BY q.created_at ASC
                LIMIT 1
                """,
                (user_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                return None

            item = dict(row)
            cursor = await conn.execute(
                "SELECT 1 FROM newspaper_category_subs WHERE user_id = ? AND category = ?",
                (user_id, item["category"]),
            )
            still_subscribed = await cursor.fetchone() is not None

            if still_subscribed:
                return item

            # Unsubscribed category: drop it and look at the next one.
            await conn.execute("DELETE FROM newspaper_delivery_queue WHERE id = ?", (item["queue_id"],))


async def update_last_newspaper_alert(user_id: int) -> None:
    async with get_connection() as conn:
        await conn.execute("UPDATE users SET last_newspaper_alert_at = ? WHERE id = ?", (_now(), user_id))
