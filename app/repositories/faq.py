"""FAQ: типы вопросов и ответы, видимые клиенту."""

from __future__ import annotations

from typing import Any, cast

from sqlalchemy import func, select, text, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import FaqItem, FaqType


async def types(session: AsyncSession) -> list[FaqType]:
    result = await session.execute(
        select(FaqType).where(FaqType.is_active).order_by(FaqType.sort_order, FaqType.title)
    )
    return list(result.scalars())


async def all_types(session: AsyncSession) -> list[FaqType]:
    result = await session.execute(select(FaqType).order_by(FaqType.sort_order, FaqType.title))
    return list(result.scalars())


async def items(session: AsyncSession, type_id: int) -> list[FaqItem]:
    result = await session.execute(
        select(FaqItem)
        .where(FaqItem.type_id == type_id, FaqItem.is_active)
        .order_by(FaqItem.sort_order, FaqItem.question)
    )
    return list(result.scalars())


async def orphan_items(session: AsyncSession) -> list[FaqItem]:
    result = await session.execute(
        select(FaqItem)
        .where(FaqItem.type_id.is_(None), FaqItem.is_active)
        .order_by(FaqItem.sort_order, FaqItem.id)
    )
    return list(result.scalars())


async def all_active_items(session: AsyncSession) -> list[FaqItem]:
    result = await session.execute(
        select(FaqItem).where(FaqItem.is_active).order_by(FaqItem.sort_order, FaqItem.id)
    )
    return list(result.scalars())


async def item(session: AsyncSession, item_id: int) -> FaqItem | None:
    found = await session.get(FaqItem, item_id)
    return found if found is not None and found.is_active else None


async def visible_item(session: AsyncSession, item_id: int) -> FaqItem | None:
    result = await session.execute(
        select(FaqItem)
        .join(FaqType, FaqType.id == FaqItem.type_id)
        .where(FaqItem.id == item_id, FaqItem.is_active, FaqType.is_active)
    )
    return result.scalar_one_or_none()


async def next_type_id(session: AsyncSession) -> int:
    await session.execute(text("select pg_advisory_xact_lock(716201001)"))
    value = await session.scalar(select(func.coalesce(func.max(FaqType.id), 0) + 1))
    return int(value or 1)


async def create_type(
    session: AsyncSession,
    title: str,
    *,
    sort_order: int | None = None,
    type_id: int | None = None,
) -> FaqType:
    assigned_id = type_id if type_id is not None else await next_type_id(session)
    assigned_order = sort_order
    if assigned_order is None:
        next_order_value = await session.scalar(
            select(func.coalesce(func.max(FaqType.sort_order), -1) + 1).where(FaqType.is_active)
        )
        assigned_order = int(next_order_value or 0)
    faq_type = FaqType(
        id=assigned_id,
        title=title,
        is_active=True,
        sort_order=assigned_order,
    )
    session.add(faq_type)
    await session.flush()
    return faq_type


async def update_type_order(session: AsyncSession, *, type_id: int, sort_order: int) -> bool:
    value = await session.get(FaqType, type_id)
    if value is None or not value.is_active:
        return False
    value.sort_order = sort_order
    await session.flush()
    return True


async def create_item(
    session: AsyncSession,
    *,
    type_id: int,
    question: str,
    answer: str,
    sort_order: int = 0,
) -> FaqItem:
    value = FaqItem(
        type_id=type_id,
        question=question,
        answer=answer,
        is_active=True,
        sort_order=sort_order,
    )
    session.add(value)
    await session.flush()
    return value


async def disable_type(session: AsyncSession, type_id: int) -> int:
    value = await session.get(FaqType, type_id)
    if value is None or not value.is_active:
        return -1
    value.is_active = False
    result = cast(
        CursorResult[Any],
        await session.execute(
            update(FaqItem)
            .where(FaqItem.type_id == type_id, FaqItem.is_active)
            .values(type_id=None)
        ),
    )
    await session.flush()
    return int(result.rowcount or 0)


async def disable_item(session: AsyncSession, item_id: int) -> bool:
    value = await session.get(FaqItem, item_id)
    if value is None or not value.is_active:
        return False
    value.is_active = False
    await session.flush()
    return True


async def assign_item(session: AsyncSession, *, item_id: int, type_id: int) -> bool:
    value = await session.get(FaqItem, item_id)
    target = await session.get(FaqType, type_id)
    if value is None or not value.is_active or target is None or not target.is_active:
        return False
    value.type_id = type_id
    await session.flush()
    return True
