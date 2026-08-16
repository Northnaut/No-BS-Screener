import logging

from app.database.connection import get_connection

logger = logging.getLogger(__name__)

_DDL_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tg_id INTEGER UNIQUE NOT NULL,
        username TEXT NULL,
        is_active INTEGER NOT NULL DEFAULT 1,
        youtube_shorts_enabled INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sources (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        platform TEXT NOT NULL,
        external_id TEXT NOT NULL,
        title TEXT NULL,
        url TEXT NOT NULL,
        last_checked_at TEXT NULL,
        is_bootstrapped INTEGER NOT NULL DEFAULT 0,
        fail_count INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        UNIQUE(platform, external_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS subscriptions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
        created_at TEXT NOT NULL,
        UNIQUE(user_id, source_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS seen_posts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
        post_external_id TEXT NOT NULL,
        title TEXT NOT NULL,
        text TEXT NOT NULL DEFAULT '',
        url TEXT NOT NULL,
        is_important INTEGER NULL,
        summary TEXT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(source_id, post_external_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_seen_source_created
    ON seen_posts(source_id, created_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_seen_unclassified
    ON seen_posts(created_at) WHERE is_important IS NULL
    """,
]


async def _ensure_column(conn, table: str, column: str, column_def: str) -> None:
    cursor = await conn.execute(f"PRAGMA table_info({table})")
    existing_columns = {row[1] for row in await cursor.fetchall()}
    if column not in existing_columns:
        await conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_def}")
        logger.info("Migrated: added column %s.%s", table, column)


async def init_db() -> None:
    async with get_connection() as conn:
        for statement in _DDL_STATEMENTS:
            await conn.execute(statement)
        await _ensure_column(conn, "seen_posts", "text", "TEXT NOT NULL DEFAULT ''")
        await _ensure_column(conn, "users", "youtube_shorts_enabled", "INTEGER NOT NULL DEFAULT 1")
        await _ensure_column(conn, "seen_posts", "summary_degen", "TEXT NULL")
        await _ensure_column(conn, "users", "summary_style", "TEXT NOT NULL DEFAULT 'brief'")
    logger.info("Database initialized (schema ensured)")
