import logging
import time
from collections import OrderedDict
from typing import Any, Awaitable, Callable, Dict

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Update

logger = logging.getLogger(__name__)

_DEDUP_TTL_SECONDS = 300
_MAX_TRACKED_UPDATES = 5000


class DeduplicationMiddleware(BaseMiddleware):
    def __init__(self) -> None:
        self._seen: "OrderedDict[int, float]" = OrderedDict()

    def _prune_expired(self, now: float) -> None:
        while self._seen:
            oldest_id, seen_at = next(iter(self._seen.items()))
            if now - seen_at > _DEDUP_TTL_SECONDS:
                self._seen.popitem(last=False)
            else:
                break
        while len(self._seen) > _MAX_TRACKED_UPDATES:
            self._seen.popitem(last=False)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        if not isinstance(event, Update):
            return await handler(event, data)

        now = time.monotonic()
        self._prune_expired(now)

        logger.info("INCOMING update_id=%s type=%s", event.update_id, event.event_type)

        if event.update_id in self._seen:
            logger.warning("Duplicate update_id=%s ignored", event.update_id)
            return None

        self._seen[event.update_id] = now
        return await handler(event, data)
