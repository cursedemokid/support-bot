"""Запомненные служебные сообщения текущего экрана Telegram."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ChatUiMessage


async def list_messages(session: AsyncSession, chat_id: int) -> list[ChatUiMessage]:
    result = await session.execute(select(ChatUiMessage).where(ChatUiMessage.chat_id == chat_id))
    return list(result.scalars())


async def remember(
    session: AsyncSession, *, chat_id: int, message_id: int, created_at: dt.datetime
) -> None:
    session.add(ChatUiMessage(chat_id=chat_id, message_id=message_id, created_at=created_at))
    await session.flush()


async def forget_all(session: AsyncSession, chat_id: int) -> None:
    await session.execute(delete(ChatUiMessage).where(ChatUiMessage.chat_id == chat_id))
