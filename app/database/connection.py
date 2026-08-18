import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

import aiosqlite

from app.config import DB_PATH

logger = logging.getLogger(__name__)

# In WAL mode, closing the LAST open connection triggers a full checkpoint and tears down
# the -wal/-shm files. Because every query here opens and closes its own connection and they
# rarely overlap, essentially every single write was paying a full checkpoint — measured at
# 16.4ms per write, against 0.18ms once a connection stays resident. This keep-alive holds
# one idle connection open for the process lifetime purely so the WAL is never the last one
# out; it is never used for queries, so it can't serialize anything.
_keepalive: aiosqlite.Connection | None = None


async def init_connection_pragmas() -> None:
    """Applies the once-per-database settings and opens the WAL keep-alive. Called once at
    startup. journal_mode is persisted in the database file itself, so re-asserting it on
    every connection was pure overhead — it takes an exclusive lock to verify, which alone
    accounted for ~2.35ms of the 16.4ms per-write cost."""
    global _keepalive
    if _keepalive is not None:
        return
    try:
        conn = await aiosqlite.connect(DB_PATH)
        await conn.execute("PRAGMA journal_mode = WAL")
        # NORMAL is the recommended pairing with WAL: durable across process crashes, and
        # only risks the last commits on an OS-level crash/power loss. For a feed cache
        # that is a fine trade for removing an fsync from every write.
        await conn.execute("PRAGMA synchronous = NORMAL")
        await conn.commit()
        _keepalive = conn
        logger.info("Database WAL keep-alive connection established")
    except Exception:
        logger.exception("Failed to establish the database keep-alive connection at %s", DB_PATH)
        _keepalive = None


async def close_connection_pragmas() -> None:
    """Releases the keep-alive on shutdown, allowing a final clean checkpoint."""
    global _keepalive
    if _keepalive is None:
        return
    try:
        await _keepalive.close()
    except Exception:
        logger.exception("Failed to close the database keep-alive connection")
    finally:
        _keepalive = None


@asynccontextmanager
async def get_connection() -> AsyncIterator[aiosqlite.Connection]:
    try:
        conn = await aiosqlite.connect(DB_PATH)
    except Exception:
        logger.exception("Failed to open database connection at %s", DB_PATH)
        raise

    conn.row_factory = aiosqlite.Row
    try:
        # foreign_keys is per-connection (not persisted), so it still has to be set here.
        # A longer busy_timeout makes concurrent writers wait their turn instead of
        # immediately raising "database is locked" — needed now that the newspaper
        # discovery cycle fans out concurrently via asyncio.gather.
        await conn.execute("PRAGMA foreign_keys = ON")
        await conn.execute("PRAGMA busy_timeout = 10000")
        yield conn
        await conn.commit()
    except Exception:
        try:
            await conn.rollback()
        except Exception:
            # Never let a failed rollback mask the original error.
            logger.exception("Rollback failed while handling a database error")
        logger.exception("Database operation failed, transaction rolled back")
        raise
    finally:
        try:
            await conn.close()
        except Exception:
            logger.exception("Failed to close a database connection")
