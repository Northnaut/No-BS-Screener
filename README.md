# No BS Screener

A Telegram bot that watches Reddit, YouTube, Telegram channels, and ~38 curated
newspaper RSS feeds so you don't have to — then uses AI to filter out noise and
memes, alerting you only when something is genuinely important.

Importance is judged by the **real-world scale of the actor involved**, not by
keywords or topic — the same filter works whether you're tracking crypto,
politics, tech, or any other subject.

## How it works

1. **Add a source.** Paste a Reddit, YouTube, or Telegram link (or several at
   once for bulk add) via the bot's inline menu. Newspapers are pre-curated —
   you subscribe to a whole category instead of individual feeds.
2. **Discovery.** A scheduler polls every source on an interval, pulling new
   posts/videos/articles and recording them in a dedup ledger so nothing is
   processed twice.
3. **Classification.** New posts are batched (one AI call per platform group,
   not per post) and sent to an LLM, which scores each 0–10 for importance and
   writes a summary. Score ≥ 6 = important; below that is dropped as noise.
   Newspaper posts skip the importance filter (the source list is already
   curated) and only get summarized.
4. **Delivery.** Important posts are queued and dispatched to subscribers
   through a single shared outgoing queue, throttled so a backlog (e.g. after
   downtime) can't dump dozens of alerts on someone at once.

## Features

- **Multi-platform ingestion** — Reddit (via RSS, with JSON as fallback),
  YouTube channels, Telegram channels (via a Telethon userbot/MTProto client,
  since bot accounts can't read channel history), and curated newspaper RSS
  feeds across Economy, Crypto, Politics, and Tech.
- **Topic-agnostic AI filter** — no hardcoded keyword lists; the model judges
  newsworthiness by the scale/stature of who or what is involved.
- **Dual AI provider with fallback** — Mistral as primary, Groq
  (`openai/gpt-oss-20b`) as fallback if Mistral is unavailable or rate-limited.
  A batch is only ever discarded as NOISE by an actual model verdict — never
  silently on a transport failure, which instead leaves posts unclassified for
  retry on the next cycle.
- **Five summary styles per user** — Original, TL;DR (brief), Casual (degen),
  ELI5, and TikTok — switchable anytime from the bot menu.
- **Per-user newspaper delivery queue** — capped and throttled independently
  of the discovery cycle, so 38 feeds don't turn into a spam firehose; the
  queue favors fresh headlines over stale backlog.
- **Stale-alert protection** — posts older than a configurable age at
  discovery time (e.g. after the bot was offline) are marked seen but not
  alerted, so downtime doesn't dump an hours-old backlog on subscribers.
- **Bulk add / unsubscribe-all** flows, inline keyboards for menu navigation.

## Architecture

```
app/
  main.py              Entry point — wires up aiogram bot, APScheduler jobs, userbot
  config.py             Env-driven configuration, all defaults documented inline
  ai/
    client.py           Mistral/Groq batch classification & summarization, provider fallback
    prompts.py           System/user prompt templates
  parsers/
    base.py              FetchedPost dataclass shared by all parsers
    reddit.py, youtube.py, telegram.py, newspapers.py
    validators.py         Source URL/link validation
  services/
    poller.py             Discovery cycles, classification batch job, cleanup
    notifier.py            Outgoing alert dispatch (shared throttled queue)
    userbot.py             Telethon MTProto client lifecycle
  handlers/               aiogram command/callback handlers (menus, subscriptions)
  keyboards/               Inline keyboard layouts
  middlewares/dedup.py     Update deduplication middleware
  database/
    schema.py              DDL + lightweight migrations (ALTER TABLE on startup)
    queries.py              All SQL access
    connection.py           aiosqlite connection/pragma management
  states/                  aiogram FSM states (add-source flow)
  utils/                    Logging, formatting, Telegram helpers
scripts/
  telegram_login.py       One-time interactive script to generate TG_SESSION_STRING
tests/                    pytest test suite
```

**Storage:** SQLite (`bot.db`, WAL mode) via `aiosqlite`. Schema is created and
migrated automatically on startup (`init_db()`), no separate migration tool.

**Scheduling:** `APScheduler` (`AsyncIOScheduler`) runs discovery, newspaper
discovery, classification batching, newspaper alert dispatch, and periodic
cleanup as independent interval jobs, all with `max_instances=1` so a slow
cycle can't overlap itself.

## Setup

### Requirements

- Python 3.10+
- A Telegram bot token ([@BotFather](https://t.me/BotFather))
- A [Mistral](https://console.mistral.ai/) API key (primary AI provider)
- A [Groq](https://console.groq.com/) API key (fallback provider, optional but recommended)
- A Telegram API ID/hash + user session string, **only if** you want to track
  Telegram channel sources (see below)

### Install

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # Linux/macOS

pip install -r requirements.txt
```

### Configure

Copy `.env.example` to `.env` and fill in the values:

```bash
cp .env.example .env
```

| Variable | Required | Description |
|---|---|---|
| `BOT_TOKEN` | Yes | Telegram bot token from BotFather |
| `MISTRAL_API_KEY` | Yes | Primary AI provider |
| `GROQ_API_KEY` | No | Fallback AI provider |
| `DB_PATH` | No | SQLite file path (default `bot.db`) |
| `POLL_INTERVAL_MINUTES` | No | Reddit/YouTube/Telegram discovery interval (default 15) |
| `NEWSPAPER_POLL_INTERVAL_MINUTES` | No | Newspaper discovery interval (default 6) |
| `POSTS_PER_FETCH` | No | Posts pulled per source per cycle (default 25) |
| `NEWSPAPER_SOURCES_PER_CYCLE` | No | How many of the ~38 newspaper feeds are checked per cycle (default 5) |
| `NEWSPAPER_ALERT_INTERVAL_MINUTES` | No | Per-user newspaper alert throttle (default 15) |
| `NEWSPAPER_QUEUE_MAX_PER_USER` | No | Per-user newspaper backlog cap (default 8) |
| `CLASSIFICATION_BATCH_INTERVAL_MINUTES` | No | AI batching interval (default 2) |
| `CLASSIFICATION_BATCH_MAX_POSTS` | No | Max posts per AI batch call (default 25) |
| `MAX_POST_AGE_HOURS` | No | Posts older than this at discovery are marked seen but not alerted (default 3) |
| `OUTGOING_ALERT_INTERVAL_SECONDS` | No | Minimum spacing between any two outgoing alerts (default 300) |
| `TG_API_ID` / `TG_API_HASH` / `TG_SESSION_STRING` | No | Telethon userbot credentials — only needed to track Telegram channels |

Every variable, including its tuning rationale, is documented inline in
`app/config.py` and `.env.example`.

### Telegram channel tracking (optional)

Bot accounts can't read Telegram channel history, so channel sources are
fetched through a real user account (MTProto) via Telethon. To enable it:

```bash
python scripts/telegram_login.py
```

Run this yourself in your own terminal — the login code is sent straight to
your Telegram app/SMS, so it can't be completed by an automated agent. It
prints a session string; put it in `.env` as `TG_SESSION_STRING` along with
`TG_API_ID`/`TG_API_HASH` (from https://my.telegram.org/apps). If left unset,
Telegram sources are simply skipped.

### Run

```bash
python -m app.main
```

## Testing

```bash
pytest
```

Configuration lives in `pytest.ini` (`asyncio_mode = auto`, tests discovered
under `tests/`).

## Notes

- `bot.db` / `bot.db-wal` / `bot.db-shm` and `bot.log` are runtime artifacts,
  not part of the source — check `.gitignore` before committing.
- Newspaper feed sources are the single source of truth in
  `app/parsers/newspapers.py::NEWSPAPER_FEEDS` and are re-synced into the
  database on every startup.
