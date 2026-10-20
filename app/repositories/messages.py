"""Репозиторий истории сообщений."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import MessageHistory
from app.repositories import tickets as tickets_repo


async def add_message(
    session: AsyncSession,
    ticket_id: int,
    from_user_id: int,
    message_text: str,
    sent_at: dt.datetime,
    photo_file_id: str | None = None,
) -> MessageHistory:
    """Добавить сообщение в историю тикета."""
    msg = MessageHistory(
        ticket_id=ticket_id,
        from_user_id=from_user_id,
        message_text=message_text,
        photo_file_id=photo_file_id,
        sent_at=sent_at,
    )
    session.add(msg)
    await tickets_repo.touch_last_message(session, ticket_id=ticket_id, sent_at=sent_at)
    await session.flush()
    return msg


async def get_history(session: AsyncSession, ticket_id: int) -> list[MessageHistory]:
    """Получить всю историю переписки по тикету."""
    stmt = (
        select(MessageHistory)
        .where(MessageHistory.ticket_id == ticket_id)
        .order_by(MessageHistory.sent_at)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())
