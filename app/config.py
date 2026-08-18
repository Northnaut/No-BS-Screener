import os
import sys

from dotenv import load_dotenv

load_dotenv()


def _require(name: str) -> str:
    value = os.getenv(name)
    if not value:
        print(f"[CONFIG ERROR] Missing required environment variable: {name}. Check your .env file.", file=sys.stderr)
        sys.exit(1)
    return value


BOT_TOKEN: str = _require("BOT_TOKEN")
MISTRAL_API_KEY: str = _require("MISTRAL_API_KEY")
GROQ_API_KEY: str = os.getenv("GROQ_API_KEY", "")
DB_PATH: str = os.getenv("DB_PATH", "bot.db")
POLL_INTERVAL_MINUTES: int = int(os.getenv("POLL_INTERVAL_MINUTES", "15"))
NEWSPAPER_POLL_INTERVAL_MINUTES: int = int(os.getenv("NEWSPAPER_POLL_INTERVAL_MINUTES", "6"))
POSTS_PER_FETCH: int = int(os.getenv("POSTS_PER_FETCH", "25"))

# Newspapers are discovered/classified continuously in the background (see above), but a
# single user is only ever sent one newspaper alert per this interval, drawn from their
# personal delivery queue — this is what keeps ~38 curated feeds from turning into a spam
# firehose for a subscriber. NEWSPAPER_QUEUE_MAX_PER_USER caps how many pending newspaper
# posts pile up per user while they're waiting; once exceeded, the oldest queued posts are
# dropped in favor of fresher ones (a stale headline is worth less than a spam-free feed).
NEWSPAPER_ALERT_INTERVAL_MINUTES: int = int(os.getenv("NEWSPAPER_ALERT_INTERVAL_MINUTES", "15"))
# At one alert per 15 minutes a user drains 4 items/hour, so a 40-item queue is a 10-HOUR
# backlog — and since the queue trims to the newest while the dispatcher serves the oldest,
# every subscriber was permanently reading headlines ~10 hours stale while fresher ones
# queued behind them. 8 caps the backlog at ~2 hours, which keeps "news" actually news.
NEWSPAPER_QUEUE_MAX_PER_USER: int = int(os.getenv("NEWSPAPER_QUEUE_MAX_PER_USER", "8"))

# The classification worker processes one post at a time across every platform combined,
# so polling all ~39 newspaper feeds every cycle floods it with far more posts than it can
# keep up with, and the backlog of never-classified posts grows without bound. Instead,
# each discovery cycle only checks a random sample of this many newspaper sources (still
# drawn from the full seeded list) — the rest get their turn on a later cycle.
NEWSPAPER_SOURCES_PER_CYCLE: int = int(os.getenv("NEWSPAPER_SOURCES_PER_CYCLE", "5"))

# Posts land in the classification queue continuously as sources are discovered, but instead
# of calling the AI once per post, a scheduled job batches everything queued since the last
# run into a single AI request per platform group (newspapers vs. reddit/telegram) — far fewer
# calls for the same throughput. CLASSIFICATION_BATCH_MAX_POSTS caps how many posts go into one
# batch call; if more are queued, the oldest are drained first and the rest wait for the next cycle.
# Batch size is a blast-radius decision as much as a throughput one: a single malformed or
# truncated AI reply affects the whole batch at once, and an 80-post batch (~43k input
# tokens worst case) is also beyond what the Groq fallback will accept — so the fallback
# would fail exactly when it's needed. 25 every 2 minutes sustains 750 posts/hour per
# platform group, well above observed intake, at a quarter of the exposure.
CLASSIFICATION_BATCH_INTERVAL_MINUTES: int = int(os.getenv("CLASSIFICATION_BATCH_INTERVAL_MINUTES", "2"))
CLASSIFICATION_BATCH_MAX_POSTS: int = int(os.getenv("CLASSIFICATION_BATCH_MAX_POSTS", "25"))

# Telegram userbot (MTProto) credentials, used to fetch posts from Telegram channels.
# Optional: if unset, Telegram channel sources are skipped. See scripts/telegram_login.py.
TG_API_ID: str = os.getenv("TG_API_ID", "")
TG_API_HASH: str = os.getenv("TG_API_HASH", "")
TG_SESSION_STRING: str = os.getenv("TG_SESSION_STRING", "")

# After downtime (bot off overnight, restarted for maintenance, etc.), the next discovery
# cycle claims every post published while it was off as "new" — that's correct, they really
# are new to the dedup ledger, but by the time they're classified and sent they can be hours
# stale. Posts older than this at discovery time are marked seen (never re-checked) but
# silently skipped instead of alerted, so a long gap gets swallowed instead of dumped.
MAX_POST_AGE_HOURS: int = int(os.getenv("MAX_POST_AGE_HOURS", "3"))

# Reddit/Telegram/YouTube alerts used to broadcast the instant a post was classified
# important, with no pacing between different posts — a backlog freed all at once (e.g. right
# after the MAX_POST_AGE_HOURS-old backlog clears, or several sources turning up important
# posts in the same classification batch) landed in a subscriber's chat within seconds of each
# other. All such alerts now go through one shared outgoing queue, drained at most one post
# every this many seconds, regardless of which source or platform it came from.
OUTGOING_ALERT_INTERVAL_SECONDS: int = int(os.getenv("OUTGOING_ALERT_INTERVAL_SECONDS", "300"))
