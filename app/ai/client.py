import json
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from groq import APIStatusError as GroqAPIStatusError
from groq import AsyncGroq
from groq import RateLimitError as GroqRateLimitError
from mistralai.client import Mistral
from mistralai.client.errors import SDKError as MistralSDKError

from app.ai.prompts import BATCH_SYSTEM_PROMPT, NEWSPAPER_BATCH_SYSTEM_PROMPT, build_batch_user_prompt
from app.config import GROQ_API_KEY, MISTRAL_API_KEY

logger = logging.getLogger(__name__)

_GROQ_MODEL = "openai/gpt-oss-20b"
_MISTRAL_MODEL = "ministral-8b-latest"

_groq_client = AsyncGroq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

# The Mistral SDK's httpx client defaults to a 5s timeout on every phase. A non-streaming
# completion sends no bytes until generation finishes, so that is effectively a 5s cap on
# total generation time — fine for the small batches we send today, but it would fail every
# request the moment a backlog produces full-size batches (i.e. exactly when recovering from
# an outage). The SDK also defaults to retry_config=UNSET, meaning a single attempt with no
# backoff, so one transient 429 discards the whole batch.
_MISTRAL_TIMEOUT_MS = 180_000
_mistral_client = (
    Mistral(api_key=MISTRAL_API_KEY, timeout_ms=_MISTRAL_TIMEOUT_MS) if MISTRAL_API_KEY else None
)

# Classification must be stable: the same post should not flip IMPORTANT/NOISE between runs.
# Groq in particular defaults to temperature=1.0.
_TEMPERATURE = 0.1

STYLE_KEYS = ("brief", "degen", "eli5", "tiktok")


class QuotaExceededError(Exception):
    pass


class ProvidersFailedError(Exception):
    """Every provider failed for a non-quota reason. Callers must leave the affected posts
    unclassified so they get retried, rather than persisting a synthesized verdict."""
    pass


class _ProviderUnavailable(Exception):
    def __init__(self, is_quota: bool):
        self.is_quota = is_quota
        super().__init__()


@dataclass
class ClassificationResult:
    is_important: bool
    score: int
    reason: str
    summaries: dict[str, str]


@dataclass
class SummaryResult:
    summaries: dict[str, str]


def _strip_markdown_emphasis(text: str) -> str:
    """Summaries are inserted straight into Telegram messages sent with parse_mode=HTML, which
    doesn't render markdown — a stray '*based*' or '**word**' the AI adds for emphasis shows up
    as literal asterisks instead of italics/bold. Stripping them here is a single choke point
    covering every style/route (reddit+telegram classification and newspaper summarization)."""
    return text.replace("**", "").replace("*", "")


def _extract_summaries(data: dict) -> dict[str, str]:
    return {key: _strip_markdown_emphasis(str(data.get(f"summary_{key}", ""))) for key in STYLE_KEYS}


def _empty_summaries() -> dict[str, str]:
    return {key: "" for key in STYLE_KEYS}


_IMPORTANCE_THRESHOLD = 6


def _parse_score(value: object) -> int:
    """Clamps whatever the model sent to a 0-10 int; malformed/missing scores default to 0
    (NOISE) rather than crashing or silently trusting an out-of-range value."""
    try:
        score = int(round(float(value)))
    except (TypeError, ValueError):
        return 0
    return max(0, min(10, score))


async def _call_groq(system_prompt: str, user_prompt: str) -> str:
    if _groq_client is None:
        raise _ProviderUnavailable(is_quota=False)

    try:
        completion = await _groq_client.chat.completions.create(
            model=_GROQ_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            response_format={"type": "json_object"},
            temperature=_TEMPERATURE,
        )
    except GroqRateLimitError as exc:
        raise _ProviderUnavailable(is_quota=True) from exc
    except GroqAPIStatusError as exc:
        # 413 request-too-large is the likeliest large-batch failure. It is a capacity
        # problem, not a bad batch, so treat it as retryable rather than burning the posts.
        if getattr(exc, "status_code", None) == 413:
            raise _ProviderUnavailable(is_quota=True) from exc
        logger.exception("Groq API error")
        raise _ProviderUnavailable(is_quota=False) from exc
    except Exception as exc:
        logger.exception("Unexpected error calling Groq")
        raise _ProviderUnavailable(is_quota=False) from exc

    content = completion.choices[0].message.content
    if content is None:
        raise _ProviderUnavailable(is_quota=False)
    return content


async def _call_mistral(system_prompt: str, user_prompt: str) -> str:
    if _mistral_client is None:
        raise _ProviderUnavailable(is_quota=False)

    try:
        completion = await _mistral_client.chat.complete_async(
            model=_MISTRAL_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            response_format={"type": "json_object"},
            temperature=_TEMPERATURE,
        )
    except MistralSDKError as exc:
        if exc.status_code == 429:
            raise _ProviderUnavailable(is_quota=True) from exc
        logger.exception("Mistral API error")
        raise _ProviderUnavailable(is_quota=False) from exc
    except Exception as exc:
        logger.exception("Unexpected error calling Mistral")
        raise _ProviderUnavailable(is_quota=False) from exc

    content = completion.choices[0].message.content
    if content is None:
        raise _ProviderUnavailable(is_quota=False)
    return content


_PROVIDERS: list[tuple[str, Callable[[str, str], Awaitable[str]]]] = []
if MISTRAL_API_KEY:
    _PROVIDERS.append(("Mistral", _call_mistral))
else:
    logger.warning("MISTRAL_API_KEY not set; running without a primary Mistral provider")
if GROQ_API_KEY:
    _PROVIDERS.append(("Groq", _call_groq))
else:
    logger.warning("GROQ_API_KEY not set; running without a Groq fallback provider")


async def _run_providers_batch(system_prompt: str, user_prompt: str, label: str) -> tuple[Optional[list], bool]:
    """Tries each configured provider in order. Returns (results_list, False) on success,
    or (None, any_provider_was_quota_exhausted) if every provider failed."""
    # ANY provider hitting quota makes the batch retryable — not just whichever happened to
    # fail last. Tracking only the last one meant provider ordering decided whether data was
    # retried or discarded: Mistral 429 followed by a Groq network blip looked like a hard
    # failure and burned the batch.
    any_quota = False
    for name, call in _PROVIDERS:
        try:
            response_text = await call(system_prompt, user_prompt)
        except _ProviderUnavailable as exc:
            logger.warning("%s unavailable for batch of %s (quota=%s)", name, label, exc.is_quota)
            any_quota = any_quota or exc.is_quota
            continue

        try:
            data = json.loads(response_text)
            if not isinstance(data, dict) or not isinstance(data.get("results"), list):
                raise TypeError(f"Expected a JSON object with a 'results' list, got {type(data).__name__}")
            return data["results"], False
        except (json.JSONDecodeError, AttributeError, TypeError):
            # Truncated to keep a ~40KB malformed payload out of the log file.
            logger.warning(
                "Failed to parse %s batch response for %s: %.500r", name, label, response_text
            )
            continue

    return None, any_quota


async def classify_posts_batch(posts: list[dict]) -> dict[int, ClassificationResult]:
    """posts: list of {"id": int, "platform": str, "title": str, "text": str}. Classifies the
    whole batch in a single AI call. Any id missing from the AI's response falls back to NOISE
    so one malformed/dropped entry can't stall the queue forever."""
    if not posts:
        return {}

    user_prompt = build_batch_user_prompt(posts)
    label = f"{len(posts)} post(s)"
    results, quota_exhausted = await _run_providers_batch(BATCH_SYSTEM_PROMPT, user_prompt, label)

    if results is None:
        if quota_exhausted:
            logger.warning("All AI providers exhausted their quota while classifying a batch of %d post(s)", len(posts))
            raise QuotaExceededError("All configured AI providers are rate-limited")
        # Never persist a transport/parse failure as a real editorial verdict. Returning
        # is_important=False here used to write NOISE to the DB, which took the rows out of
        # the unclassified set permanently — real news silently deleted by a network blip.
        # Raising leaves them NULL so the next cycle retries them.
        logger.error("All AI providers failed to classify a batch of %d post(s)", len(posts))
        raise ProvidersFailedError("All configured AI providers failed to classify the batch")

    if len(results) != len(posts):
        # Canary for a truncated or padded response — the one failure mode that could
        # silently attach the wrong verdict to the wrong post.
        logger.warning(
            "AI returned %d result(s) for a batch of %d post(s)", len(results), len(posts)
        )

    valid_ids = {post["id"] for post in posts}
    by_id: dict[int, ClassificationResult] = {}
    for item in results:
        if not isinstance(item, dict):
            continue
        try:
            post_id = int(item["id"])
        except (KeyError, TypeError, ValueError):
            continue
        if post_id not in valid_ids:
            logger.warning("AI returned out-of-range id %s for a batch of %d post(s)", post_id, len(posts))
            continue
        if post_id in by_id:
            logger.warning("AI returned duplicate id %s; keeping the first result", post_id)
            continue
        score = _parse_score(item.get("score"))
        by_id[post_id] = ClassificationResult(
            # score is the ground truth for the threshold rule (score >= _IMPORTANCE_THRESHOLD -> important) —
            # the model's own is_important boolean is not trusted here, since a mismatch
            # between the two would otherwise silently override the numeric threshold.
            is_important=score >= _IMPORTANCE_THRESHOLD,
            score=score,
            reason=str(item.get("reason", "")),
            summaries=_extract_summaries(item),
        )

    missing_fallback = ClassificationResult(is_important=False, score=0, reason="Missing from AI batch response", summaries=_empty_summaries())
    for post in posts:
        by_id.setdefault(post["id"], missing_fallback)

    return by_id


async def summarize_posts_batch(posts: list[dict]) -> dict[int, SummaryResult]:
    """Newspaper posts skip importance triage entirely (the source list is already curated) —
    this only rewrites each one into the alert styles, one AI call for the whole batch."""
    if not posts:
        return {}

    user_prompt = build_batch_user_prompt(posts)
    label = f"{len(posts)} newspaper post(s)"
    results, quota_exhausted = await _run_providers_batch(NEWSPAPER_BATCH_SYSTEM_PROMPT, user_prompt, label)

    if results is None:
        if quota_exhausted:
            logger.warning("All AI providers exhausted their quota while summarizing a batch of %d newspaper post(s)", len(posts))
            raise QuotaExceededError("All configured AI providers are rate-limited")
        # Same reasoning as classify_posts_batch: returning empty summaries here used to save
        # the posts as important-with-blank-bodies AND queue them for delivery, so a failed
        # AI call shipped degraded alerts to users instead of retrying.
        logger.error("All AI providers failed to summarize a batch of %d newspaper post(s)", len(posts))
        raise ProvidersFailedError("All configured AI providers failed to summarize the batch")

    if len(results) != len(posts):
        logger.warning(
            "AI returned %d result(s) for a newspaper batch of %d post(s)", len(results), len(posts)
        )

    valid_ids = {post["id"] for post in posts}
    by_id: dict[int, SummaryResult] = {}
    for item in results:
        if not isinstance(item, dict):
            continue
        try:
            post_id = int(item["id"])
        except (KeyError, TypeError, ValueError):
            continue
        if post_id not in valid_ids:
            logger.warning("AI returned out-of-range id %s for a newspaper batch", post_id)
            continue
        if post_id in by_id:
            logger.warning("AI returned duplicate id %s; keeping the first result", post_id)
            continue
        by_id[post_id] = SummaryResult(summaries=_extract_summaries(item))

    missing_fallback = SummaryResult(summaries=_empty_summaries())
    for post in posts:
        by_id.setdefault(post["id"], missing_fallback)

    return by_id
