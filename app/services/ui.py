"""Отрисовка одноэкранного интерфейса в Telegram.

Telegram не умеет заменить пачку сообщений одним действием, поэтому перед
каждой новой отрисовкой удаляем сообщения, которые бот сам записал в БД.
Ошибки удаления ожидаемы (сообщение уже удалено/старше лимита) и не мешают
показать новый экран.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import InlineKeyboardMarkup
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.clock import Clock
from app.media import resolve_media
from app.repositories import ui as ui_repo

TELEGRAM_TEXT_LIMIT = 4096
TELEGRAM_CAPTION_LIMIT = 1024


def telegram_length(text: str) -> int:
    """Консервативная длина HTML в UTF-16, включая разметку и entities."""
    return len(text.encode("utf-16-le")) // 2


@dataclass(frozen=True, slots=True)
class UiPhoto:
    file_id: str
    caption: str


class UiService:
    def __init__(self, clock: Clock) -> None:
        self._clock = clock

    async def clear(self, session: AsyncSession, bot: Bot, chat_id: int) -> None:
        for saved in await ui_repo.list_messages(session, chat_id):
            try:
                await bot.delete_message(chat_id, saved.message_id)
            except (TelegramBadRequest, TelegramForbiddenError) as exc:
                logger.debug(
                    "Экранное сообщение {}/{} не удалено: {}", chat_id, saved.message_id, exc
                )
        await ui_repo.forget_all(session, chat_id)

    async def remove_incoming(self, bot: Bot, message_chat_id: int, message_id: int) -> None:
        with suppress(TelegramBadRequest, TelegramForbiddenError):
            await bot.delete_message(message_chat_id, message_id)

    async def render(
        self,
        session: AsyncSession,
        bot: Bot,
        *,
        chat_id: int,
        parts: Sequence[str | UiPhoto],
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> None:
        await self.clear(session, bot, chat_id)
        actions: list[str | UiPhoto] = []
        for part in parts:
            if isinstance(part, str):
                actions.extend(_split(part))
            elif telegram_length(part.caption) > TELEGRAM_CAPTION_LIMIT:
                actions.append(UiPhoto(part.file_id, ""))
                actions.extend(_split(part.caption))
            else:
                actions.append(part)
        actions = actions or [" "]
        for index, action in enumerate(actions):
            markup = reply_markup if index == len(actions) - 1 else None
            if isinstance(action, UiPhoto):
                sent = await bot.send_photo(
                    chat_id,
                    resolve_media(action.file_id),
                    caption=action.caption,
                    reply_markup=markup,
                )
            else:
                sent = await bot.send_message(chat_id, action, reply_markup=markup)
            await ui_repo.remember(
                session,
                chat_id=chat_id,
                message_id=sent.message_id,
                created_at=self._clock.now(),
            )
        # Middleware deliberately leaves transaction ownership to handlers.
        # Экранное состояние бессмысленно откатывать при закрытии сессии.
        await session.commit()


def _split(text: str) -> list[str]:
    """Разделить HTML, не разрывая entity и закрывая теги в каждом сообщении."""
    if telegram_length(text) <= TELEGRAM_TEXT_LIMIT:
        return [text]
    chunks: list[str] = []
    stack: list[tuple[str, str]] = []
    current = ""
    current_length = 0
    # Все динамические поля уже escaped; HTML здесь — разметка шаблонов.
    for match in re.finditer(r"<[^>]+>|&(?:#\d+|#x[\da-fA-F]+|[a-zA-Z]+);|.", text, re.S):
        token = match.group()
        if token.startswith("</"):
            stack.pop()
            current += token
            current_length += telegram_length(token)
            continue
        opening = re.fullmatch(r"<([a-zA-Z0-9_-]+)(?:\s[^>]*)?>", token)
        closing = "".join(end for _, end in reversed(stack))
        extra_end = f"</{opening[1]}>" if opening else ""
        if current_length + telegram_length(token + closing + extra_end) > TELEGRAM_TEXT_LIMIT:
            chunks.append(current + closing)
            current = "".join(start for start, _ in stack)
            current_length = telegram_length(current)
        current += token
        current_length += telegram_length(token)
        if opening:
            stack.append((token, extra_end))
    if current:
        chunks.append(current)
    return chunks
