"""Репозиторий маршрутов сообщений: message_id → тикет."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import MessageRoute


async def remember(
    session: AsyncSession,
    chat_id: int,
    message_id: int,
    ticket_id: int,
    created_at: dt.datetime,
) -> None:
    """Запомнить, что сообщение бота в чате относится к тикету."""
    session.add(
        MessageRoute(
            chat_id=chat_id,
            message_id=message_id,
            ticket_id=ticket_id,
            created_at=created_at,
        )
    )
    await session.flush()


async def resolve(session: AsyncSession, chat_id: int, message_id: int) -> int | None:
    """Тикет по сообщению, на которое ответили. None — сообщение не наше."""
    route = await session.get(MessageRoute, (chat_id, message_id))
    return None if route is None else route.ticket_id


async def forget(session: AsyncSession, chat_id: int, message_id: int) -> None:
    await session.execute(
        delete(MessageRoute).where(
            MessageRoute.chat_id == chat_id,
            MessageRoute.message_id == message_id,
        )
    )
