# NewsScreener — Software Design Document (MVP)

## 1. High-Level Concept

A Telegram bot that reduces information noise for crypto traders.

The user subscribes to sources (subreddits and YouTube channels) via an inline menu. A background scheduler polls all unique sources on a fixed interval, fetches fresh posts/videos, runs them through an LLM classifier (Gemini), and delivers to the user only what can actually move the market — with a short summary.

Data flow:

```
User → Bot (FSM: pick platform → send link → validate → save)
                                                          ↓
                                                    [ sources ]
                                                          ↓
Scheduler (every N min) → Fetchers (Reddit .json / YouTube RSS)
                                                          ↓
                                          dedup by external_id (seen_posts)
                                                          ↓
                                              AI Classifier (Gemini)
                                                          ↓
                                    is_important? → Notifier → all subscribers
```

Key architectural decisions:

- **A source is stored once; subscriptions are many-to-many.** If 100 users are subscribed to `r/CryptoCurrency`, we fetch and classify it once and broadcast to all hundred. This saves both HTTP requests and, more importantly, the free Gemini quota.
- **Deduplication happens at the source level, not per user.** The `seen_posts` table stores each post's `external_id`; if it's already there, the post never even reaches the AI.
- **Classification results are cached in `seen_posts`** (`is_important`, `summary`), so one post equals one LLM call, forever.
- **The first poll of a new source is a "cold start"**: posts are marked as seen without sending alerts, otherwise a user would instantly get 25 notifications from the existing feed.

---

## 2. Tech Stack

| Component | Library | Purpose |
|---|---|---|
| Runtime | Python 3.10+ | asyncio stack |
| Telegram | `aiogram` 3.x | Routers, FSM, inline keyboards |
| Database | `aiosqlite` | Async SQLite, no ORM |
| HTTP | `aiohttp` | Reddit `.json` endpoints |
| RSS | `feedparser` | YouTube `feeds/videos.xml` |
| AI | `google-generativeai` | Gemini (free tier) for classification |
| Scheduler | `APScheduler` (AsyncIOScheduler) | Background source polling |
| Config | `python-dotenv` | Tokens from `.env` |

`requirements.txt`:

```
aiogram>=3.7.0
aiosqlite>=0.20.0
aiohttp>=3.9.0
feedparser>=6.0.11
google-generativeai>=0.8.0
APScheduler>=3.10.4
python-dotenv>=1.0.1
```

**Reddit note:** the public `.json` endpoint works without API keys but requires a custom `User-Agent` (otherwise 429/403) and tolerates roughly ~60 requests/min per IP. That's enough for the MVP; moving to the official OAuth API is a later concern.

**Gemini note:** the free tier is limited by RPM/RPD. The poller must therefore call the LLM sequentially (not in parallel) with a small delay, and handle `ResourceExhausted` — when the quota is exhausted, the cycle stops gracefully and unprocessed posts wait for the next tick.

---

## 3. Data Models

SQLite, four tables. `PRAGMA foreign_keys = ON` is set on every connection.

### 3.1 `users`

| Field | Type | Description |
|---|---|---|
| `id` | INTEGER PK AUTOINCREMENT | Internal id |
| `tg_id` | INTEGER UNIQUE NOT NULL | Telegram user id |
| `username` | TEXT NULL | @username at registration time |
| `is_active` | INTEGER NOT NULL DEFAULT 1 | 0 = user blocked the bot |
| `created_at` | TEXT NOT NULL | ISO-8601 UTC |

### 3.2 `sources`

Global source registry. Unique by (platform, external id).

| Field | Type | Description |
|---|---|---|
| `id` | INTEGER PK AUTOINCREMENT | |
| `platform` | TEXT NOT NULL | `reddit` \| `youtube` |
| `external_id` | TEXT NOT NULL | Reddit: `CryptoCurrency` (no `r/`). YouTube: `UCxxxx…` (channel id) |
| `title` | TEXT NULL | Human-readable name for the subscription list |
| `url` | TEXT NOT NULL | Normalized URL used for fetching |
| `last_checked_at` | TEXT NULL | Timestamp of the last successful poll |
| `is_bootstrapped` | INTEGER NOT NULL DEFAULT 0 | 0 = first poll hasn't happened yet (cold start) |
| `fail_count` | INTEGER NOT NULL DEFAULT 0 | Consecutive failure counter |
| `created_at` | TEXT NOT NULL | |

`UNIQUE(platform, external_id)`

### 3.3 `subscriptions`

| Field | Type | Description |
|---|---|---|
| `id` | INTEGER PK AUTOINCREMENT | |
| `user_id` | INTEGER NOT NULL → `users(id)` ON DELETE CASCADE | |
| `source_id` | INTEGER NOT NULL → `sources(id)` ON DELETE CASCADE | |
| `created_at` | TEXT NOT NULL | |

`UNIQUE(user_id, source_id)`

### 3.4 `seen_posts`

Deduplication + cache of the AI classification result.

| Field | Type | Description |
|---|---|---|
| `id` | INTEGER PK AUTOINCREMENT | |
| `source_id` | INTEGER NOT NULL → `sources(id)` ON DELETE CASCADE | |
| `post_external_id` | TEXT NOT NULL | Reddit: `t3_abc123`. YouTube: `yt:video:xxxx` |
| `title` | TEXT NOT NULL | |
| `url` | TEXT NOT NULL | |
| `is_important` | INTEGER NULL | NULL = not classified yet, 0/1 = verdict |
| `summary` | TEXT NULL | Gemini's summary (only when important) |
| `created_at` | TEXT NOT NULL | |

`UNIQUE(source_id, post_external_id)`
`INDEX idx_seen_source_created ON seen_posts(source_id, created_at)`

**Retention:** a scheduled maintenance job purges `seen_posts` rows older than 30 days, otherwise the table grows unbounded.

---

## 4. File / Directory Structure

```
NewsScreener/
├── .env                        # BOT_TOKEN, GEMINI_API_KEY (gitignored)
├── .env.example
├── .gitignore
├── requirements.txt
├── README.md
├── bot.db                      # SQLite, created automatically
└── app/
    ├── __init__.py
    ├── main.py                 # Entry point: init DB, Dispatcher, scheduler, polling
    ├── config.py                # .env loading, required-var validation
    │
    ├── database/
    │   ├── __init__.py
    │   ├── connection.py       # Async connection context manager, PRAGMA foreign_keys
    │   ├── schema.py           # DDL: CREATE TABLE IF NOT EXISTS + indexes
    │   └── queries.py          # All SQL operations (users, sources, subs, seen_posts)
    │
    ├── handlers/
    │   ├── __init__.py         # Registers all routers on the Dispatcher
    │   ├── commands.py         # /start, /help, /menu
    │   ├── subscriptions.py    # FSM for adding a source, listing, deleting
    │   └── errors.py           # Global aiogram error handler
    │
    ├── keyboards/
    │   ├── __init__.py
    │   └── inline.py           # Main menu, subscription list, delete buttons
    │
    ├── states/
    │   ├── __init__.py
    │   └── add_source.py       # FSM StatesGroup: waiting_for_link
    │
    ├── parsers/
    │   ├── __init__.py
    │   ├── base.py             # FetchedPost dataclass + common fetcher interface
    │   ├── validators.py       # Link parsing/normalization, YouTube handle → channel_id resolution
    │   ├── reddit.py           # aiohttp → /r/<sub>/new.json
    │   └── youtube.py          # feedparser → feeds/videos.xml?channel_id=
    │
    ├── ai/
    │   ├── __init__.py
    │   ├── client.py           # Gemini setup, retries, quota handling
    │   └── prompts.py          # Classifier system prompt + JSON response schema
    │
    ├── services/
    │   ├── __init__.py
    │   ├── poller.py           # Orchestration: fetch → dedup → classify → notify
    │   └── notifier.py         # Alert broadcast, deactivating users who blocked the bot
    │
    └── utils/
        ├── __init__.py
        ├── logger.py           # Single logging setup (file + stdout)
        └── formatters.py       # Formatting for alert text and lists
```

**Separation principle:** `handlers` know nothing about SQL or HTTP — they call into `database/queries.py` and `parsers/validators.py`. `services/poller.py` is the only place where parsers, AI, and notifications are wired together.

---

## 5. Step-by-Step Execution

The plan is split so each step is self-contained and runnable. After every stage, the bot must start without errors.

### Stage 1. Skeleton + config + database

**Files:** `.env.example`, `.gitignore`, `requirements.txt`, `app/config.py`, `app/utils/logger.py`, `app/database/{connection,schema,queries}.py`, `app/main.py` (minimal).

- `config.py`: reads `BOT_TOKEN`, `GEMINI_API_KEY`, `DB_PATH`, `POLL_INTERVAL_MINUTES`, `POSTS_PER_FETCH`. Fails with a clear message if a required variable is missing.
- `connection.py`: async context manager on top of `aiosqlite`, `row_factory = aiosqlite.Row`, `PRAGMA foreign_keys = ON`.
- `schema.py`: `init_db()` — all `CREATE TABLE IF NOT EXISTS` statements and indexes from section 3.
- `queries.py`: CRUD functions — `get_or_create_user`, `get_or_create_source`, `add_subscription`, `get_user_subscriptions`, `delete_subscription`, `get_all_active_sources`, `get_source_subscribers`, `is_post_seen`, `save_seen_post`, `mark_source_checked`.
- `main.py`: logger init, `init_db()`, starts an empty `Dispatcher` with `bot.polling`.

**Done when:** `python -m app.main` starts up and `bot.db` is created with all tables.

### Stage 2. Telegram layer: menu, add-source FSM, list, delete

**Files:** `app/keyboards/inline.py`, `app/states/add_source.py`, `app/handlers/{commands,subscriptions,errors,__init__}.py`, `app/parsers/validators.py`, `app/utils/formatters.py`.

- `/start` → registers the user + shows the main menu (`Add Reddit`, `Add YouTube`, `My Subscriptions`).
- Callback `add:reddit` / `add:youtube` → sets the FSM state `waiting_for_link`, stores the chosen platform in context, prompts for a link.
- On link input → `validators.py` parses the formats:
  - Reddit: `r/Name`, `/r/Name`, `reddit.com/r/Name`, `https://www.reddit.com/r/Name/`
  - YouTube: `youtube.com/channel/UC...`, `youtube.com/@handle`, `youtu.be` channel links
  - For `@handle` — resolve the `channel_id` over the network (GET the channel page and extract `channelId`), since the RSS feed only works by `channel_id`.
- The source's existence is verified with a real request (so garbage never lands in the DB) → `get_or_create_source` → `add_subscription`. A duplicate subscription gets a clear message, not an error.
- `My Subscriptions` → an inline list with a `❌` button next to each entry, deletion updates the message in place (`edit_message_reply_markup`).
- `errors.py`: a global handler that logs the traceback and replies to the user with a neutral message.

**Done when:** a subscription can be added/viewed/removed, and an invalid link is rejected with a message.

### Stage 3. Source parsers

**Files:** `app/parsers/{base,reddit,youtube}.py`.

- `base.py`: `@dataclass FetchedPost(external_id, title, text, url, published_at)` — a unified shape for both sources.
- `reddit.py`: `aiohttp` GET `https://www.reddit.com/r/{sub}/new.json?limit=N` with `User-Agent: CryptoAIScreener/1.0`, 15s timeout, parses `data.children[*].data` (`name`, `title`, `selftext`, `permalink`, `created_utc`). On 403/429/timeout — returns an empty list and increments `fail_count`, without crashing the cycle.
- `youtube.py`: `feedparser.parse` against `https://www.youtube.com/feeds/videos.xml?channel_id={id}`. Since `feedparser` is synchronous/blocking, it's called via `asyncio.to_thread`. Fields used: `entry.id`, `entry.title`, `entry.summary`, `entry.link`, `entry.published`.

**Done when:** a manual run of each fetcher prints a list of fresh posts.

### Stage 4. AI classification

**Files:** `app/ai/{client,prompts}.py`.

- `prompts.py`: system prompt casting the model as a crypto analyst; input: title + text + platform; output strictly JSON:
  ```json
  {"is_important": true, "reason": "...", "summary": "..."}
  ```
  "Important" criteria: listings, hacks/exploits, regulatory events (SEC/ETF/lawsuits), large whale movements, major partnerships, network forks/upgrades, exchange bankruptcies. "Noise" criteria: memes, price predictions, "when moon", beginner questions, referral spam.
- `client.py`: configures `google-generativeai`, calls it via `asyncio.to_thread` (the SDK is synchronous), uses `response_mime_type="application/json"` to guarantee parseable output, handles `json.JSONDecodeError` by treating the post as not important, and raises a dedicated `QuotaExceededError` on quota exhaustion, which the poller catches to gracefully stop the cycle until the next tick.

**Done when:** `classify_post()` returns correct verdicts on a test set of 2 posts (a meme and an SEC news item).

### Stage 5. Poller, notifier, final wiring

**Files:** `app/services/{poller,notifier}.py`, final `app/main.py`.

- `poller.py`, the `run_polling_cycle()` loop:
  1. `get_all_active_sources()` — only sources with at least one subscriber.
  2. For each source — fetch by `platform`.
  3. Filter out already-seen posts via `is_post_seen`.
  4. If `is_bootstrapped == 0`: save all posts as seen with `is_important = 0`, set the flag, **send nothing**.
  5. Otherwise: classify new posts sequentially (~1s pause between Gemini calls), store the verdict in `seen_posts`.
  6. Important posts → `notifier.broadcast(source_id, post)`.
  7. `mark_source_checked`, reset/increment `fail_count`.
  8. The whole cycle is wrapped in try/except — one source failing must not crash the rest.
- `notifier.py`: fetches a source's subscribers, sends the alert (title, platform, summary, link), throttles to ~20 messages/sec, and on `TelegramForbiddenError` sets `is_active = 0` for that user.
- `main.py`: `AsyncIOScheduler`, job `run_polling_cycle` every `POLL_INTERVAL_MINUTES` (default 15) with `max_instances=1`, plus a daily job to purge `seen_posts` rows older than 30 days. Clean shutdown of the `aiohttp` session and the scheduler.

**Done when:** the bot runs autonomously — subscribing to a subreddit, waiting two cycles, receiving an alert only for a genuinely important post, and never receiving the same post twice.

---

## 6. Post-MVP

- Per-user AI sensitivity threshold configuration.
- Twitter/X support via Nitter instances.
- Batching posts into a single Gemini request to save quota.
- Migration from SQLite to PostgreSQL as the number of sources grows.
