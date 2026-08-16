from aiogram import Dispatcher

from app.handlers.commands import router as commands_router
from app.handlers.errors import router as errors_router
from app.handlers.subscriptions import router as subscriptions_router


def register_handlers(dp: Dispatcher) -> None:
    dp.include_router(commands_router)
    dp.include_router(subscriptions_router)
    dp.include_router(errors_router)
