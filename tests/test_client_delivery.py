"""Свободный ответ клиента проходит через настоящий сервис и PostgreSQL."""

from __future__ import annotations

from typing import cast

from aiogram import Bot

from app.bot.handlers import user_flow
from app.repositories import messages as messages_repo
from app.repositories import tickets as tickets_repo
from app.services.tickets import TicketService
from tests.factories import FakeBot, admin_callback, admin_fsm, private_message

CLIENT_ID = 4242
MANAGER_ID = 555


async def test_unprompted_client_photo_reaches_assigned_staff_and_history(
    app_config, clock, session_factory
) -> None:
    service = TicketService(app_config, clock)
    bot = FakeBot()
    async with session_factory() as session, session.begin():
        created = await service.create_ticket(
            session,
            cast(Bot, bot),
            telegram_user_id=CLIENT_ID,
            telegram_username="client",
            category_key="defect",
            description="Исходный вопрос",
        )
        await service.take(
            session, cast(Bot, bot), ticket_id=created.ticket.id, manager_id=MANAGER_ID
        )
    bot.sent.clear()
    message = private_message(
        None,
        user_id=CLIENT_ID,
        caption="Дополнение <&>",
        photo=[
            {"file_id": "client-photo", "file_unique_id": "unique", "width": 100, "height": 100}
        ],
    ).as_(cast(Bot, bot))
    async with session_factory() as session:
        await user_flow.handle_client_non_text(
            message,
            service,
            state=admin_fsm(CLIENT_ID),
            session=session,
            bot=cast(Bot, bot),
            clock=clock,
        )
    assert bot.photos_to(MANAGER_ID)[0].photo == "client-photo"
    assert "Дополнение &lt;&amp;&gt;" in (bot.photos_to(MANAGER_ID)[0].caption or "")
    assert bot.to(777) == []
    async with session_factory() as session:
        history = await messages_repo.get_history(session, created.ticket.id)
        assert [m.message_text for m in history] == ["Исходный вопрос", "Дополнение <&>"]
        assert history[-1].photo_file_id == "client-photo"


async def test_pending_client_message_delivered_only_to_selected_ticket(
    app_config, clock, session_factory
) -> None:
    service = TicketService(app_config, clock)
    bot = FakeBot()
    state = admin_fsm(CLIENT_ID)
    async with session_factory() as session, session.begin():
        first = await service.create_ticket(
            session,
            cast(Bot, bot),
            telegram_user_id=CLIENT_ID,
            telegram_username="client",
            category_key="defect",
            description="Первый вопрос",
        )
        second = await service.create_ticket(
            session,
            cast(Bot, bot),
            telegram_user_id=CLIENT_ID,
            telegram_username="client",
            category_key="other",
            description="Второй вопрос",
        )
    message = private_message("Уточнение", user_id=CLIENT_ID).as_(cast(Bot, bot))
    async with session_factory() as session:
        await user_flow.handle_client_message(
            message, service, state=state, session=session, bot=cast(Bot, bot), clock=clock
        )
    query = admin_callback(f"clientticket:{second.ticket.id}", CLIENT_ID).as_(cast(Bot, bot))
    async with session_factory() as session:
        await user_flow.select_ticket_for_pending_message(
            query, state, session, cast(Bot, bot), service, clock
        )
    async with session_factory() as session:
        assert len(await messages_repo.get_history(session, first.ticket.id)) == 1
        history = await messages_repo.get_history(session, second.ticket.id)
        assert [m.message_text for m in history] == ["Второй вопрос", "Уточнение"]
        assert len(await tickets_repo.list_open_for_user(session, CLIENT_ID)) == 2
    assert "pending_client_message" not in await state.get_data()
