import glob
import logging
import os
import sys
import time
from logging.handlers import RotatingFileHandler

# bot.log was growing ~5.7MB/day with no rotation. 10MB x 3 keeps roughly the last two days
# of history bounded at 40MB total.
_MAX_LOG_BYTES = 10 * 1024 * 1024
_LOG_BACKUP_COUNT = 3

_LOG_MAX_AGE_HOURS = 72

logger = logging.getLogger(__name__)


def setup_logging() -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # The console defaults to the Windows ANSI codepage, so any log line containing Cyrillic
    # post titles raised UnicodeEncodeError inside the handler and the record was dropped
    # entirely. The file handler was already utf-8; stdout was not.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    root.addHandler(stream_handler)

    file_handler = RotatingFileHandler(
        "bot.log", maxBytes=_MAX_LOG_BYTES, backupCount=_LOG_BACKUP_COUNT, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    logging.getLogger("aiogram").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)


def cleanup_old_logs(max_age_hours: int = _LOG_MAX_AGE_HOURS) -> None:
    """Delete rotated bot.log.* backups older than max_age_hours. Does not touch the
    active bot.log (kept open by RotatingFileHandler) or anything under app/, only
    rotated backups sitting in the working directory."""
    cutoff = time.time() - max_age_hours * 3600
    for path in glob.glob("bot.log.*"):
        try:
            if os.path.getmtime(path) < cutoff:
                os.remove(path)
                logger.info("Deleted stale log backup: %s", path)
        except OSError as exc:
            logger.warning("Could not delete old log backup %s: %s", path, exc)
