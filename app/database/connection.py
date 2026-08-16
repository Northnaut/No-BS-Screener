import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

import aiosqlite

from app.config import DB_PATH

logger = logging.getLogger(__name__)


@asynccontextmanager
async def get_connection() -> AsyncIterator[aiosqlite.Connection]:
    try:
        conn = await aiosqlite.connect(DB_PATH)
    except Exception:
        logger.exception("Failed to open database connection at %s", DB_PATH)
        raise

    conn.row_factory = aiosqlite.Row
    try:
        await conn.execute("PRAGMA foreign_keys = ON")
        yield conn
        await conn.commit()
    except Exception:
        await conn.rollback()
        logger.exception("Database operation failed, transaction rolled back")
        raise
    finally:
        await conn.close()
