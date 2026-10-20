"""Контекст лога на апдейт: chat_id и telegram_user_id в каждую строку.

Ни текста, ни имени пользователя здесь нет — только числовые идентификаторы,
прошедшие белый список `app.logging.event_context`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject
from loguru import logger


def _identifiers(event: TelegramObject) -> dict[str, int]:
    fields: dict[str, int] = {}
    chat = getattr(event, "chat", None)
    if chat is not None:
        fields["chat_id"] = chat.id
    user = getattr(event, "from_user", None)
    if user is not None:
        fields["telegram_user_id"] = user.id
    return fields


class LoggingContextMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        with logger.contextualize(**_identifiers(event)):
            return await handler(event, data)
