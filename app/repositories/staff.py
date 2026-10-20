"""Реестр сотрудников и их роли."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import StaffMember

ADMIN = "admin"
SUPPORT = "support"
ROLES = frozenset({ADMIN, SUPPORT})


def normalize_username(value: str) -> str:
    return value.strip().lstrip("@").lower()


async def active_by_telegram_id(session: AsyncSession, telegram_user_id: int) -> StaffMember | None:
    result = await session.execute(
        select(StaffMember).where(
            StaffMember.telegram_user_id == telegram_user_id, StaffMember.is_active
        )
    )
    return result.scalar_one_or_none()


async def active_staff(session: AsyncSession) -> list[StaffMember]:
    result = await session.execute(
        select(StaffMember).where(StaffMember.is_active).order_by(StaffMember.display_name)
    )
    return list(result.scalars())


async def all_staff(session: AsyncSession) -> list[StaffMember]:
    result = await session.execute(select(StaffMember).order_by(StaffMember.display_name))
    return list(result.scalars())


async def invite(
    session: AsyncSession,
    *,
    username: str,
    display_name: str,
    role: str,
    now: dt.datetime,
) -> StaffMember:
    if role not in ROLES:
        raise ValueError("Неизвестная роль")
    item = StaffMember(
        telegram_username=normalize_username(username),
        display_name=display_name.strip() or f"@{normalize_username(username)}",
        role=role,
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    session.add(item)
    await session.flush()
    return item


async def activate_invitation(
    session: AsyncSession, *, telegram_user_id: int, username: str, now: dt.datetime
) -> StaffMember | None:
    normalized = normalize_username(username)
    result = await session.execute(
        select(StaffMember).where(
            StaffMember.telegram_username == normalized,
            StaffMember.telegram_user_id.is_(None),
            StaffMember.is_active,
        )
    )
    item = result.scalar_one_or_none()
    if item is None:
        return None
    item.telegram_user_id = telegram_user_id
    item.updated_at = now
    await session.flush()
    return item


async def update_member(
    session: AsyncSession,
    *,
    member_id: int,
    display_name: str | None = None,
    role: str | None = None,
    is_active: bool | None = None,
    now: dt.datetime,
) -> StaffMember | None:
    item = await session.get(StaffMember, member_id)
    if item is None:
        return None
    if display_name is not None:
        item.display_name = display_name.strip() or item.display_name
    if role is not None:
        if role not in ROLES:
            raise ValueError("Неизвестная роль")
        item.role = role
    if is_active is not None:
        item.is_active = is_active
    item.updated_at = now
    await session.flush()
    return item
