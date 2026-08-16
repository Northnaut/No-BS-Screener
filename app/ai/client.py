import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable

from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types
from groq import APIStatusError as GroqAPIStatusError
from groq import AsyncGroq
from groq import RateLimitError as GroqRateLimitError

from app.ai.prompts import SYSTEM_PROMPT, build_user_prompt
from app.config import GEMINI_API_KEY, GROQ_API_KEY

logger = logging.getLogger(__name__)

_GEMINI_MODEL = "gemini-flash-lite-latest"
_GROQ_MODEL = "llama-3.1-8b-instant"
_SERVER_ERROR_RETRY_ATTEMPTS = 3
_SERVER_ERROR_RETRY_DELAY_SECONDS = 3

_gemini_client = genai.Client(api_key=GEMINI_API_KEY)
_groq_client = AsyncGroq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None


class QuotaExceededError(Exception):
    pass


class _ProviderUnavailable(Exception):
    def __init__(self, is_quota: bool):
        self.is_quota = is_quota
        super().__init__()


@dataclass
class ClassificationResult:
    is_important: bool
    reason: str
    summary: str


def _parse_is_important(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    if isinstance(value, (int, float)):
        return bool(value)
    return False


async def _call_gemini(user_prompt: str) -> str:
    response = None
    for attempt in range(_SERVER_ERROR_RETRY_ATTEMPTS):
        try:
            response = await _gemini_client.aio.models.generate_content(
                model=_GEMINI_MODEL,
                contents=user_prompt,
                config=genai_types.GenerateContentConfig(
                    system_instruction=SYSTEM_PROMPT,
                    response_mime_type="application/json",
                ),
            )
            break
        except genai_errors.ClientError as exc:
            if exc.code == 429:
                raise _ProviderUnavailable(is_quota=True) from exc
            logger.exception("Gemini client error")
            raise _ProviderUnavailable(is_quota=False) from exc
        except genai_errors.ServerError as exc:
            if attempt < _SERVER_ERROR_RETRY_ATTEMPTS - 1:
                logger.warning("Gemini server error, retrying (attempt %d)", attempt + 1)
                await asyncio.sleep(_SERVER_ERROR_RETRY_DELAY_SECONDS)
                continue
            logger.exception("Gemini server error (out of retries)")
            raise _ProviderUnavailable(is_quota=False) from exc

    try:
        return response.text
    except Exception as exc:
        logger.exception("Gemini response has no readable text (likely blocked by safety filters)")
        raise _ProviderUnavailable(is_quota=False) from exc


async def _call_groq(user_prompt: str) -> str:
    if _groq_client is None:
        raise _ProviderUnavailable(is_quota=False)

    try:
        completion = await _groq_client.chat.completions.create(
            model=_GROQ_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            response_format={"type": "json_object"},
        )
    except GroqRateLimitError as exc:
        raise _ProviderUnavailable(is_quota=True) from exc
    except GroqAPIStatusError as exc:
        logger.exception("Groq API error")
        raise _ProviderUnavailable(is_quota=False) from exc
    except Exception as exc:
        logger.exception("Unexpected error calling Groq")
        raise _ProviderUnavailable(is_quota=False) from exc

    content = completion.choices[0].message.content
    if content is None:
        raise _ProviderUnavailable(is_quota=False)
    return content


_PROVIDERS: list[tuple[str, Callable[[str], Awaitable[str]]]] = [("Gemini", _call_gemini)]
if GROQ_API_KEY:
    _PROVIDERS.append(("Groq", _call_groq))
else:
    logger.warning("GROQ_API_KEY not set; running without an AI fallback provider")


async def classify_post(platform: str, title: str, text: str) -> ClassificationResult:
    user_prompt = build_user_prompt(platform, title, text)

    last_was_quota = False
    for name, call in _PROVIDERS:
        try:
            response_text = await call(user_prompt)
        except _ProviderUnavailable as exc:
            logger.warning("%s unavailable for post '%s' (quota=%s)", name, title, exc.is_quota)
            last_was_quota = exc.is_quota
            continue

        try:
            data = json.loads(response_text)
            return ClassificationResult(
                is_important=_parse_is_important(data.get("is_important", False)),
                reason=str(data.get("reason", "")),
                summary=str(data.get("summary", "")),
            )
        except (json.JSONDecodeError, AttributeError, TypeError):
            logger.exception("Failed to parse %s response for post '%s': %r", name, title, response_text)
            last_was_quota = False
            continue

    if last_was_quota:
        logger.warning("All AI providers exhausted their quota while classifying post '%s'", title)
        raise QuotaExceededError("All configured AI providers are rate-limited")

    logger.error("All AI providers failed to classify post '%s'", title)
    return ClassificationResult(is_important=False, reason="All AI providers failed", summary="")
