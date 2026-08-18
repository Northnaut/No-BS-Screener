# No BS Screener — Software Design Document (Final)

Status: product is complete. From here on, only maintenance and small fixes — no new features planned.

## 1. High-Level Concept

A Telegram bot that cuts information noise for traders and anyone following markets/crypto/world news. The user subscribes to sources of four types — Reddit, YouTube, Telegram channels, and curated newspapers/news RSS feeds — via an inline menu. Background schedulers poll the sources, every new post goes through AI, and the user only receives what's actually important, in the summary style they picked.

Data flow for Reddit / YouTube / Telegram (require AI importance filtering):

```
User → Bot (FSM: platform → link → validation → save)
                                                          ↓
                                                    [ sources ]
                                                          ↓
Scheduler (every N min) → Fetchers (Reddit RSS/JSON, YouTube RSS, Telegram userbot)
                                                          ↓
                                          dedup by external_id (seen_posts)
                                                          ↓
                            AI classify_posts_batch() → is_important? + 4 summary styles, per post
                                                          ↓
                                    is_important? → Notifier → all subscribers of the source
```

Data flow for newspapers — no importance filter, styling only, delivery is queued and rate-limited per user:

```
Seed on bot startup → [ sources, platform=newspaper ]
                                                          ↓
Scheduler (every N min) → fetch_newspaper_feed() (RSS, concurrency semaphore)
                                                          ↓
                                          dedup by external_id (seen_posts)
                                                          ↓
                    AI summarize_posts_batch() → only 4 summary styles, is_important always True
                                                          ↓
                    Fan-out into newspaper_delivery_queue, one row per CATEGORY subscriber
                                                          ↓
     Dispatcher (every 1 min) → pops ≤1 post per due user → send → users.last_newspaper_alert_at
```

Key architectural decisions (unchanged since MVP, confirmed in practice):

- **A source is stored once, subscriptions are many-to-many.** Classifying one post is one AI request, regardless of how many subscribers it has.
- **Deduplication at the source level** via `seen_posts` (`UNIQUE(source_id, post_external_id)`), `INSERT OR IGNORE` — guarantees "claim once" even under parallel poll cycles.
- **Classification is asynchronous, via a queue, and batched.** Discovery cycles only fetch posts and insert them into `seen_posts` with `is_important = NULL` ("claim"); a separate scheduled job (`run_classification_batch`, every `CLASSIFICATION_BATCH_INTERVAL_MINUTES`, default 5) reads up to `CLASSIFICATION_BATCH_MAX_POSTS` unprocessed posts per platform group and classifies the whole batch in a single AI call (one for newspapers, one for reddit/telegram) instead of one call per post. This decouples the fetch rate from the analysis rate, keeps the poller from blocking on AI latency, and keeps AI request volume flat regardless of how many posts land in a cycle — anything past the batch cap just waits for the next tick.
- **One AI call = all 4 styles at once.** `summaries: dict[str, str]` with keys `brief`, `degen`, `eli5`, `tiktok` is built from a single JSON response from the model — not one call per style.
- **Newspapers are not filtered by importance** — the source list is already curated (serious publications only), so AI there just rewrites the post in the chosen style instead of deciding whether it matters.
- **Newspaper subscriptions are by category, not by source.** The user toggles 4 categories (`economy`, `crypto`, `politics`, `tech`) instead of individual publications — with 38 sources, per-source subscription would be unusable.
- **Newspaper delivery is queued and throttled per user, decoupled from discovery/AI throughput.** With ~38 curated feeds discovered continuously, sending a message the instant a post is classified would spam a subscriber every few seconds. Instead, a classified post is fanned out into `newspaper_delivery_queue` (one row per category subscriber); a dispatcher tick pops at most one post per user who's gone `NEWSPAPER_ALERT_INTERVAL_MINUTES` (default 15) since their last newspaper alert. Nothing is silently lost — unpopped posts just wait for the next due tick — except when a user's personal queue exceeds `NEWSPAPER_QUEUE_MAX_PER_USER` (default 40), in which case the oldest queued posts are dropped in favor of fresher ones.
- **Per-source cold start:** the first poll of a new source marks posts as seen without sending alerts (`is_bootstrapped`), otherwise a new subscriber would get blasted with the entire feed at once.
- **Two-provider AI fallback.** Mistral is the primary provider, Groq is the automatic fallback on unavailability/quota. Order and switching logic live in `app/ai/client.py::_run_providers_batch`, transparent to the rest of the code.

---

## 2. Tech Stack

| Component | Library | Purpose |
|---|---|---|
| Runtime | Python 3.10+ | asyncio stack |
| Telegram (bot) | `aiogram` 3.x | Routers, FSM, inline keyboards |
| Telegram (userbot) | `Telethon` | MTProto client for reading posts from Telegram channels (the public bot API doesn't allow this) |
| DB | `aiosqlite` | Async SQLite, no ORM, WAL mode |
| HTTP | `aiohttp` | Reddit JSON/RSS, newspaper RSS feeds |
| RSS | `feedparser` | YouTube and Reddit RSS, newspaper feeds — single format-agnostic date parsing |
| AI (primary) | `mistralai` | Mistral — importance classification + generating 4 summary styles |
| AI (fallback) | `groq` | Groq (Llama) — same contract, kicks in when Mistral is unavailable |
| Scheduler | `APScheduler` (AsyncIOScheduler) | Background polling: discovery (Reddit/YouTube/Telegram), newspapers, cleanup |
| Config | `python-dotenv` | Tokens and settings from `.env` |
| Tests | `pytest`, `pytest-asyncio` | Unit tests for parsers and validators |

`requirements.txt`:

```
aiogram>=3.7.0
aiosqlite>=0.20.0
aiohttp>=3.9.0
feedparser>=6.0.11
mistralai>=2.9.3
groq>=0.11.0
APScheduler>=3.10.4
python-dotenv>=1.0.1
Telethon>=1.36.0
```

**Reddit:** primary fetch method is RSS (`.../new/.rss`), with a fallback to the JSON endpoint on failure. A custom `User-Agent` is mandatory. Dates are parsed via `feedparser`'s `published_parsed`/`updated_parsed` (format-agnostic) with a fallback to the current time — a post is never dropped because of a date issue.

**Newspapers:** concurrent fetching of all 38 sources is bounded by an `asyncio.Semaphore` (6 concurrent requests) — without the limit, resolving DNS for that many hosts at once reliably hits `socket.gaierror` on most requests. Feed timeout is 12s (some publications, e.g. WaPo, consistently respond in 8-10.5s), plus 2 retries.

**Telegram channels:** fetched via an MTProto userbot (Telethon), because public Telegram channels have no open RSS/JSON API for a bot to read message history. The userbot is read-only (reads only, never sends anything under its own identity) — per the Telethon FAQ and community practice this is low-risk for account bans.

**Mistral:** if it returns a 429 (quota) or otherwise fails, Groq takes over classification entirely until access is restored.

---

## 3. Data Models

SQLite, `PRAGMA foreign_keys = ON`, `PRAGMA journal_mode = WAL`, `PRAGMA busy_timeout = 10000` — set on every connect (WAL/busy_timeout are needed because of concurrent writes from multiple simultaneous polling cycles).

### 3.1 `users`

| Field | Type | Description |
|---|---|---|
| `id` | INTEGER PK AUTOINCREMENT | Internal id |
| `tg_id` | INTEGER UNIQUE NOT NULL | Telegram user id |
| `username` | TEXT NULL | @username as of the last update |
| `is_active` | INTEGER NOT NULL DEFAULT 1 | 0 = user has blocked the bot |
| `youtube_shorts_enabled` | INTEGER NOT NULL DEFAULT 1 | Whether YouTube Shorts are included in alerts |
| `summary_style` | TEXT NOT NULL DEFAULT 'brief' | Selected summary style: `original`/`brief`/`degen`/`eli5`/`tiktok` |
| `last_newspaper_alert_at` | TEXT NULL | Timestamp of the last newspaper alert sent to this user — drives the per-user throttle |
| `created_at` | TEXT NOT NULL | ISO-8601 UTC |

### 3.2 `sources`

Global source registry — shared across Reddit/YouTube/Telegram/newspapers.

| Field | Type | Description |
|---|---|---|
| `id` | INTEGER PK AUTOINCREMENT | |
| `platform` | TEXT NOT NULL | `reddit` \| `youtube` \| `telegram` \| `newspaper` |
| `external_id` | TEXT NOT NULL | Reddit: subreddit name. YouTube: `UCxxxx…`. Telegram: channel username. Newspaper: publication name (unique key, since these sources aren't user-added) |
| `title` | TEXT NULL | Human-readable name |
| `url` | TEXT NOT NULL | Normalized URL (for newspapers — the RSS feed) |
| `category` | TEXT NULL | Newspapers only: `economy`/`crypto`/`politics`/`tech` |
| `last_checked_at` | TEXT NULL | Last successful poll |
| `is_bootstrapped` | INTEGER NOT NULL DEFAULT 0 | 0 = cold start not yet completed |
| `fail_count` | INTEGER NOT NULL DEFAULT 0 | Consecutive failure counter |
| `created_at` | TEXT NOT NULL | |

`UNIQUE(platform, external_id)`. Newspaper sources are resynced on every bot startup (`seed_newspaper_sources()`): upsert all 38 entries from `NEWSPAPER_FEEDS` + delete any that dropped out of the list (`delete_stale_newspaper_sources`).

### 3.3 `subscriptions`

Subscriptions to a specific source — used for Reddit/YouTube/Telegram.

| Field | Type | Description |
|---|---|---|
| `id` | INTEGER PK AUTOINCREMENT | |
| `user_id` | INTEGER NOT NULL → `users(id)` ON DELETE CASCADE | |
| `source_id` | INTEGER NOT NULL → `sources(id)` ON DELETE CASCADE | |
| `created_at` | TEXT NOT NULL | |

`UNIQUE(user_id, source_id)`

### 3.4 `newspaper_category_subs`

Subscriptions to newspaper CATEGORIES (not individual sources).

| Field | Type | Description |
|---|---|---|
| `id` | INTEGER PK AUTOINCREMENT | |
| `user_id` | INTEGER NOT NULL → `users(id)` ON DELETE CASCADE | |
| `category` | TEXT NOT NULL | `economy`/`crypto`/`politics`/`tech` |
| `created_at` | TEXT NOT NULL | |

`UNIQUE(user_id, category)`, indexed on `category` (for fast subscriber lookup during broadcast).

### 3.5 `seen_posts`

Deduplication + classification queue + cache of all 4 summary styles.

| Field | Type | Description |
|---|---|---|
| `id` | INTEGER PK AUTOINCREMENT | |
| `source_id` | INTEGER NOT NULL → `sources(id)` ON DELETE CASCADE | |
| `post_external_id` | TEXT NOT NULL | Unique post id on the platform |
| `title` | TEXT NOT NULL | |
| `text` | TEXT NOT NULL DEFAULT '' | Original post text (for the `original` style and as AI context) |
| `url` | TEXT NOT NULL | |
| `is_important` | INTEGER NULL | NULL = queued for classification, 0/1 = verdict (always 1 for newspapers) |
| `summary` | TEXT NULL | `brief` style |
| `summary_degen` | TEXT NULL | `degen` style |
| `summary_eli5` | TEXT NULL | `eli5` style |
| `summary_tiktok` | TEXT NULL | `tiktok` style |
| `created_at` | TEXT NOT NULL | |

`UNIQUE(source_id, post_external_id)` — this constraint is what "claim once" relies on: `INSERT OR IGNORE` when fetching a post, `is_important IS NULL` is the signal for the classification worker that a post is waiting to be processed. Index `idx_seen_unclassified ON seen_posts(created_at) WHERE is_important IS NULL` supports this worker.

Note: for newspaper posts, `text` is deliberately *not* wiped after classification (`save_seen_post(..., keep_text=True)`) — delivery is queued and can happen well after classification, and the `original` style still needs the source text at send time. Reddit/YouTube/Telegram broadcast immediately off the in-memory post text, so their `text` is cleared right after classification as before.

### 3.6 `newspaper_delivery_queue`

Per-user delivery queue for classified newspaper posts — this is what makes newspaper alerts rate-limited per subscriber instead of firing the instant a post is classified.

| Field | Type | Description |
|---|---|---|
| `id` | INTEGER PK AUTOINCREMENT | |
| `user_id` | INTEGER NOT NULL → `users(id)` ON DELETE CASCADE | |
| `seen_post_id` | INTEGER NOT NULL → `seen_posts(id)` ON DELETE CASCADE | |
| `category` | TEXT NOT NULL | Denormalized copy of the source's category at enqueue time |
| `created_at` | TEXT NOT NULL | |

`UNIQUE(user_id, seen_post_id)`, indexed on `(user_id, created_at)`. A post is fanned out into one row per subscriber of its category right after classification. Trimmed down to `NEWSPAPER_QUEUE_MAX_PER_USER` per user on every insert (oldest dropped first). Rows for a category are deleted outright when the user unsubscribes from it.

---

## 4. File / Directory Structure

```
NewsScreener/
├── .env                        # BOT_TOKEN, MISTRAL_API_KEY, GROQ_API_KEY, TG_API_ID/HASH/SESSION_STRING (gitignored)
├── .env.example
├── .gitignore
├── requirements.txt
├── pytest.ini
├── README.md
├── bot.db                      # SQLite, created automatically
└── app/
    ├── __init__.py
    ├── main.py                 # Entry point: init DB, seed newspapers, userbot, Dispatcher, 3 schedulers, classification worker
    ├── config.py                # .env loading: tokens, poll intervals, limits, Telegram userbot credentials
    │
    ├── database/
    │   ├── connection.py        # aiosqlite connection, PRAGMA foreign_keys/WAL/busy_timeout
    │   ├── schema.py             # DDL for all tables + idempotent migrations via _ensure_column
    │   └── queries.py            # All SQL operations: users, sources, subscriptions, newspaper_category_subs, seen_posts, AI triage queue, newspaper_delivery_queue
    │
    ├── handlers/
    │   ├── __init__.py           # Router registration
    │   ├── commands.py           # /start, /help
    │   ├── subscriptions.py      # Platform screens (Reddit/YouTube/Telegram), add/unsubscribe-by-button, newspapers, styles
    │   └── errors.py             # Global aiogram error handler
    │
    ├── keyboards/
    │   └── inline.py              # Main menu, platform screens, newspapers, style selection
    │
    ├── states/
    │   └── add_source.py          # FSM StatesGroup: waiting_for_link
    │
    ├── middlewares/
    │   └── dedup.py                # Deduplication of repeated Telegram updates
    │
    ├── parsers/
    │   ├── base.py                 # dataclass FetchedPost — common format across all platforms
    │   ├── validators.py           # Link parsing/normalization, resolve YouTube handle → channel_id, Reddit RSS/JSON fallback, Telegram entity resolve
    │   ├── reddit.py                # RSS (primary) + JSON (fallback) post fetching
    │   ├── youtube.py               # feedparser → feeds/videos.xml?channel_id=, Shorts filter
    │   ├── telegram.py              # Fetching posts from Telegram channels via the userbot (Telethon)
    │   └── newspapers.py            # NEWSPAPER_FEEDS (38 sources, 4 categories), seed/cleanup, concurrent fetch with semaphore
    │
    ├── ai/
    │   ├── client.py                # Mistral + Groq clients, _run_providers_batch (fallback chain), classify_posts_batch (Reddit/Telegram), summarize_posts_batch (newspapers)
    │   └── prompts.py               # Batch system prompts (regular + newspaper, no importance filter), shared description of the 4 styles, response JSON schema keyed by post id
    │
    ├── services/
    │   ├── poller.py                # run_polling_cycle (Reddit/YouTube/Telegram discovery), run_newspaper_discovery_cycle, run_classification_batch (also fans newspaper posts into the delivery queue), run_cleanup
    │   ├── notifier.py               # broadcast (by source), broadcast_video, dispatch_newspaper_alerts (drains the per-user newspaper queue at a throttled rate), deactivating users who blocked the bot
    │   └── userbot.py                # Telethon client: start/stop, flood-wait cooldown
    │
    └── utils/
        ├── logger.py                 # Central logging setup
        ├── formatters.py              # Alert formatting by style, subscription list, newspaper source list
        └── telegram.py                 # safe_edit_text — fallback for photo-caption messages on edit
```

**Tests:**

```
tests/
├── conftest.py                       # Atom/RSS-2.0 XML builders, aiohttp fakes (FakeResponse/FakeSession)
├── test_normalize_reddit_url.py
├── test_validators_reddit.py
├── test_extract_rss_body_text.py
├── test_fetch_via_rss.py             # Reddit RSS fetching, including format-agnostic date parsing
├── test_fetch_reddit_posts.py
└── test_fetch_newspaper_feed.py       # Newspaper fetching: timeouts, retries, broken feeds
```

87 tests, all green (`pytest tests/ -q`). Covers RSS/Atom parsing for both platforms on synthetic feeds (no network) plus link normalization/validation.

**Separation principle:** `handlers` know nothing about SQL or HTTP — they go through `database/queries.py` and `parsers/validators.py`. `services/poller.py` is the only place where parsers, the AI queue, and broadcasting come together.

---

## 5. AI Classification and Styles

`app/ai/prompts.py` — two batch system prompts:

- `BATCH_SYSTEM_PROMPT` — for Reddit/Telegram: judges post importance (topic-agnostic, judged by the scale of the actor/event, not tied to a specific topic like crypto) + generates all 4 summary styles, for every post in the batch at once.
- `NEWSPAPER_BATCH_SYSTEM_PROMPT` — for newspapers: importance is not judged (sources are already curated), only generates the 4 styles, for every post in the batch at once.

`build_batch_user_prompt` serializes a list of posts as `{"posts": [{"id": ..., "platform": ..., "title": ..., "body": ...}, ...]}`; the model is instructed to process each post independently and reply with `{"results": [{"id": ..., ...}, ...]}`, one entry per input post matched back by `id` (wrapped in an object, not a bare array, so Groq/Mistral's `json_object` response mode accepts it). Ids missing from the response fall back to NOISE / empty summaries in `app/ai/client.py` so one malformed entry can't stall the rest of the batch or the queue.

Both use a shared `_STYLE_INSTRUCTIONS` block describing the 4 styles:

| Key | UI name | Description |
|---|---|---|
| `original` | 📰 Original | The post's original text, no AI processing |
| `brief` | ⚡ TL;DR | Dry, short, to-the-point summary |
| `degen` | 💬 Casual | Casual crypto-trader slang |
| `eli5` | 🧒 ELI5 | Explain Like I'm 5 — extremely simple/childlike language, can be humorous |
| `tiktok` | 🎵 TikTok | Western TikTok/Gen-Z slang (mogging, maxxing, rizz, no cap, based, ratio, etc.) |

One AI call per batch returns JSON with all 4 styles for every post at once (`summary_brief`, `summary_degen`, `summary_eli5`, `summary_tiktok`) — not a separate call per style, and not a separate call per post. The `original` style requires no AI at all — it's taken directly from `seen_posts.text`.

`app/ai/client.py::_run_providers_batch` — the shared fallback chain: tries Mistral, then Groq; on unavailability (quota/error) moves to the next provider; if every provider is unavailable due to quota, raises `QuotaExceededError` (the classification batch job defers the whole batch to the next tick, `CLASSIFICATION_BATCH_INTERVAL_MINUTES` later); if every provider returns an invalid response, every post in the batch is marked not important and the styles are set to empty strings (handled by the fallback in `formatters.py`).

Style selection is a personal user setting (`users.summary_style`), applied uniformly across all 4 source platforms.

---

## 6. Telegram Bot: Menu Structure

**Main menu** (`menu:main`): 👽 Reddit · ▶️ YouTube · ✈️ Telegram · 📰 Newspapers · 🎨 Styles.

**Platform screen** (`platform:{reddit|youtube|telegram}`) — a single screen for both adding and managing subscriptions:
- ➕ Add — starts the `waiting_for_link` FSM, accepts a link/username, validates with a real request to the platform.
- 🗑 Unsubscribe — shown only if there are subscriptions; opens the subscription list as rows of buttons, clicking a button instantly unsubscribes from that specific source.
- 🗑 Unsubscribe from ALL — shown only if there are subscriptions; asks for confirmation.
- 🎬 YouTube Shorts: ON/OFF — YouTube screen only, a toggle.
- ⬅️ Back — to the main menu.

**Newspapers screen** (`menu:newspapers`):
- 📚 Sources — a reference list of all 38 publications across 4 categories, with hyperlinks to their websites (not to the RSS feeds).
- 4 category toggle buttons (💰 Economy & Markets, 🪙 Crypto & Web3, 🌍 World & Politics, 🤖 Tech & AI) — multi-select, checkmark when subscribed.
- ⬅️ Back.

**Styles screen** (`menu:styles`) — choose one of 5 styles (Original/TL;DR/Casual/ELI5/TikTok), applied globally to the user's account.

---

## 7. Schedulers (APScheduler, `app/main.py`)

| Job | Interval | What it does |
|---|---|---|
| `discovery_cycle` | `POLL_INTERVAL_MINUTES` (default 15) | `run_polling_cycle` — fetches new posts from Reddit/YouTube/Telegram, claims them into `seen_posts` |
| `newspaper_discovery_cycle` | `NEWSPAPER_POLL_INTERVAL_MINUTES` (default 6) | `run_newspaper_discovery_cycle` — concurrent fetch of all 38 newspaper feeds through a semaphore (6 at a time) |
| `newspaper_alert_dispatch` | 1 minute | `dispatch_newspaper_alerts` — for each user due (≥ `NEWSPAPER_ALERT_INTERVAL_MINUTES` since their last newspaper alert) with a non-empty queue, pops and sends exactly one post |
| `classification_batch` | `CLASSIFICATION_BATCH_INTERVAL_MINUTES` (default 5) | `run_classification_batch` — reads up to `CLASSIFICATION_BATCH_MAX_POSTS` (default 80) posts with `is_important IS NULL` per platform group and classifies each group in a single AI call (`classify_posts_batch` for Reddit/Telegram, `summarize_posts_batch` for newspapers), saves the results, and sends alerts (Reddit/Telegram) or fans posts out into `newspaper_delivery_queue` (newspapers — see §3.6). Whatever exceeds the batch cap waits for the next tick; actual newspaper *delivery* pacing is handled separately by `newspaper_alert_dispatch`. |
| `cleanup` | 24 hours | `run_cleanup` — removes old `seen_posts` records |

All jobs use `max_instances=1, coalesce=True`: missed ticks are collapsed, cycles never stack on top of each other.

---

## 8. Known Limitations (not bugs, deliberate trade-offs)

- Two newspaper feeds (CNBC Top News, CNBC Finance) consistently return an empty body with HTTP 200 — a source-side issue, not a fetching one.
- CryptoSlate sits behind a Cloudflare bot-challenge — its RSS is simply not served to bots, no workaround.
- Mistral can occasionally rate-limit (429) or error out — Groq carries the entire classification load alone during that time.

All of the above is known, documented, and the decision is to leave it alone unless it becomes a real problem.
