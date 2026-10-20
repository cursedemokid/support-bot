"""Репозиторий тикетов: CRUD без бизнес-логики.

Только работа с БД, никаких уведомлений — их отправляет service.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Ticket, TicketAssignment
from app.domain import TicketState


async def create_ticket(
    session: AsyncSession,
    telegram_user_id: int,
    telegram_username: str | None,
    category: str,
    description: str,
    title: str,
    created_at: dt.datetime,
) -> Ticket:
    """Создать новый тикет в состоянии NEW."""
    ticket = Ticket(
        guid=uuid.uuid4(),
        telegram_user_id=telegram_user_id,
        telegram_username=telegram_username,
        category=category,
        description=description,
        title=title,
        state=TicketState.NEW.value,
        created_at=created_at,
        last_message_at=created_at,
    )
    session.add(ticket)
    await session.flush()
    return ticket


async def get_ticket(session: AsyncSession, ticket_id: int) -> Ticket | None:
    """Получить тикет по ID."""
    return await session.get(Ticket, ticket_id)


async def list_open_tickets(session: AsyncSession, limit: int = 50) -> list[Ticket]:
    """Список открытых тикетов (NEW + ASSIGNED), от новых к старым."""
    stmt = (
        select(Ticket)
        .where(Ticket.state.in_([TicketState.NEW.value, TicketState.ASSIGNED.value]))
        .order_by(Ticket.last_message_at.desc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def list_closed_tickets(session: AsyncSession, limit: int = 50) -> list[Ticket]:
    result = await session.execute(
        select(Ticket)
        .where(Ticket.state == TicketState.CLOSED.value)
        .order_by(Ticket.last_message_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def list_open_for_user(session: AsyncSession, telegram_user_id: int) -> list[Ticket]:
    """Открытые тикеты одного клиента, от новых к старым."""
    stmt = (
        select(Ticket)
        .where(
            Ticket.telegram_user_id == telegram_user_id,
            Ticket.state.in_([TicketState.NEW.value, TicketState.ASSIGNED.value]),
        )
        .order_by(Ticket.last_message_at.desc())
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def list_for_user(
    session: AsyncSession, telegram_user_id: int, limit: int = 50
) -> list[Ticket]:
    """Все обращения клиента, новые сверху."""
    result = await session.execute(
        select(Ticket)
        .where(Ticket.telegram_user_id == telegram_user_id)
        .order_by(Ticket.last_message_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def list_all(session: AsyncSession, limit: int = 100) -> list[Ticket]:
    result = await session.execute(
        select(Ticket).order_by(Ticket.last_message_at.desc()).limit(limit)
    )
    return list(result.scalars().all())


async def assign_ticket(
    session: AsyncSession,
    ticket_id: int,
    manager_id: int,
    assigned_at: dt.datetime,
) -> Ticket | None:
    """Атомарно взять новый тикет; ``None`` означает, что его уже забрали."""
    result = await session.execute(
        update(Ticket)
        .where(Ticket.id == ticket_id, Ticket.state == TicketState.NEW.value)
        .values(
            state=TicketState.ASSIGNED.value,
            manager_id=manager_id,
            assigned_at=assigned_at,
        )
        .returning(Ticket.id)
    )
    if result.scalar_one_or_none() is None:
        return None
    session.add(
        TicketAssignment(
            ticket_id=ticket_id,
            staff_telegram_user_id=manager_id,
            assigned_by_telegram_user_id=manager_id,
            assigned_at=assigned_at,
        )
    )
    await session.flush()
    return await session.get(Ticket, ticket_id, populate_existing=True)


async def touch_last_message(
    session: AsyncSession, *, ticket_id: int, sent_at: dt.datetime
) -> None:
    await session.execute(
        update(Ticket).where(Ticket.id == ticket_id).values(last_message_at=sent_at)
    )


async def list_unassigned(session: AsyncSession, limit: int = 50) -> list[Ticket]:
    result = await session.execute(
        select(Ticket)
        .where(Ticket.state == TicketState.NEW.value)
        .order_by(Ticket.last_message_at.desc())
        .limit(limit)
    )
    return list(result.scalars())


async def list_assigned_to(
    session: AsyncSession, telegram_user_id: int, limit: int = 50
) -> list[Ticket]:
    result = await session.execute(
        select(Ticket)
        .where(Ticket.state == TicketState.ASSIGNED.value, Ticket.manager_id == telegram_user_id)
        .order_by(Ticket.last_message_at.desc())
        .limit(limit)
    )
    return list(result.scalars())


async def list_closed_for_staff(
    session: AsyncSession, telegram_user_id: int, limit: int = 50
) -> list[Ticket]:
    result = await session.execute(
        select(Ticket)
        .outerjoin(TicketAssignment, TicketAssignment.ticket_id == Ticket.id)
        .where(
            Ticket.state == TicketState.CLOSED.value,
            or_(
                TicketAssignment.staff_telegram_user_id == telegram_user_id,
                Ticket.manager_id == telegram_user_id,
            ),
        )
        .distinct()
        .order_by(Ticket.last_message_at.desc())
        .limit(limit)
    )
    return list(result.scalars())


async def was_assigned_to(session: AsyncSession, *, ticket_id: int, telegram_user_id: int) -> bool:
    result = await session.execute(
        select(Ticket.id)
        .outerjoin(TicketAssignment, TicketAssignment.ticket_id == Ticket.id)
        .where(
            Ticket.id == ticket_id,
            or_(
                TicketAssignment.staff_telegram_user_id == telegram_user_id,
                (Ticket.state == TicketState.CLOSED.value)
                & (Ticket.manager_id == telegram_user_id),
            ),
        )
        .limit(1)
    )
    return result.scalar_one_or_none() is not None


async def close_ticket(
    session: AsyncSession,
    ticket_id: int,
    closed_at: dt.datetime,
    manager_id: int,
) -> Ticket | None:
    """Атомарно закрыть назначенное исполнителю обращение и его назначение."""
    result = await session.execute(
        update(Ticket)
        .where(
            Ticket.id == ticket_id,
            Ticket.state == TicketState.ASSIGNED.value,
            Ticket.manager_id == manager_id,
        )
        .values(state=TicketState.CLOSED.value, closed_at=closed_at)
        .returning(Ticket.id)
    )
    if result.scalar_one_or_none() is None:
        return None
    await session.execute(
        update(TicketAssignment)
        .where(TicketAssignment.ticket_id == ticket_id, TicketAssignment.ended_at.is_(None))
        .values(ended_at=closed_at)
    )
    await session.flush()
    return await session.get(Ticket, ticket_id, populate_existing=True)


async def transfer_ticket(
    session: AsyncSession,
    *,
    ticket_id: int,
    from_manager_id: int,
    to_manager_id: int,
    transferred_at: dt.datetime,
) -> Ticket | None:
    ticket = await get_ticket(session, ticket_id)
    if (
        ticket is None
        or ticket.state != TicketState.ASSIGNED.value
        or ticket.manager_id != from_manager_id
    ):
        return None
    await session.execute(
        update(TicketAssignment)
        .where(
            TicketAssignment.ticket_id == ticket_id,
            TicketAssignment.staff_telegram_user_id == from_manager_id,
            TicketAssignment.ended_at.is_(None),
        )
        .values(ended_at=transferred_at)
    )
    ticket.manager_id = to_manager_id
    ticket.assigned_at = transferred_at
    session.add(
        TicketAssignment(
            ticket_id=ticket_id,
            staff_telegram_user_id=to_manager_id,
            assigned_by_telegram_user_id=from_manager_id,
            assigned_at=transferred_at,
        )
    )
    await session.flush()
    return ticket
