"""Жизненный цикл тикета на живой базе: создание → взятие → ответ → закрытие.

Проверяется поведение, видимое клиенту и менеджеру: что записано в базу,
куда ушли карточки и ответы, почему отказ. Требует TEST_DATABASE_URL.
"""

from __future__ import annotations

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import AppConfig
from app.db.models import Ticket
from app.domain import TicketState
from app.repositories import messages as messages_repo
from app.repositories import tickets as tickets_repo
from app.services.tickets import TicketService
from tests.factories import USER_ID, FakeBot

CLIENT_ID = 4242
MANAGER_ID = 555
OTHER_MANAGER_ID = 777


async def _service(app_config: AppConfig, clock) -> TicketService:
    return TicketService(app_config, clock)


async def _create(
    session_factory: async_sessionmaker[AsyncSession],
    service: TicketService,
    bot: FakeBot,
    *,
    category: str = "defect",
    description: str = "Не включается после зарядки",
    user_id: int = CLIENT_ID,
):
    async with session_factory() as session, session.begin():
        return await service.create_ticket(
            session,
            bot,
            telegram_user_id=user_id,
            telegram_username="anya",
            category_key=category,
            description=description,
        )


async def test_create_ticket_records_client_and_manager_card(
    app_config: AppConfig, session_factory, clock
) -> None:
    service = await _service(app_config, clock)
    bot = FakeBot()

    created = await _create(session_factory, service, bot)

    assert created.ticket.id > 0
    assert created.ticket.state == TicketState.NEW.value
    assert created.notified is True
    # Карточка приходит менеджерам и bootstrap-администраторам в личку.
    expected = {
        *app_config.settings.support.manager_user_ids,
        *app_config.settings.support.admin_user_ids,
    }
    assert {item.chat.id for item in bot.sent} == expected
    assert "Не включается после зарядки" in bot.sent[0].text
    assert created.ticket.guid is not None


async def test_create_ticket_writes_description_to_history(
    app_config: AppConfig, session_factory, clock
) -> None:
    service = await _service(app_config, clock)
    created = await _create(session_factory, service, FakeBot())

    async with session_factory() as session:
        history = await messages_repo.get_history(session, created.ticket.id)

    assert [m.message_text for m in history] == ["Не включается после зарядки"]
    assert history[0].from_user_id == CLIENT_ID


async def test_create_ticket_escapes_client_html_in_manager_card(
    app_config: AppConfig, session_factory, clock
) -> None:
    service = await _service(app_config, clock)
    bot = FakeBot()

    await _create(
        session_factory,
        service,
        bot,
        description="<b>Не включается</b> & <сломано>",
    )

    assert "&lt;b&gt;Не включается&lt;/b&gt; &amp; &lt;сломано&gt;" in bot.sent[0].text
    assert "<сломано>" not in bot.sent[0].text


async def test_create_ticket_with_photo_keeps_caption_and_notifies_managers(
    app_config: AppConfig, session_factory, clock
) -> None:
    service = await _service(app_config, clock)
    bot = FakeBot()

    async with session_factory() as session, session.begin():
        created = await service.create_ticket(
            session,
            bot,
            telegram_user_id=CLIENT_ID,
            telegram_username="anya",
            category_key="defect",
            description="Фото разъёма",
            photo_file_id="telegram-photo-id",
        )

    async with session_factory() as session:
        history = await messages_repo.get_history(session, created.ticket.id)
    assert history[0].photo_file_id == "telegram-photo-id"
    assert bot.photos_to(MANAGER_ID)[0].caption is not None


async def test_new_tickets_receive_distinct_internal_guids(
    app_config: AppConfig, session_factory, clock
) -> None:
    service = await _service(app_config, clock)
    bot = FakeBot()
    first = await _create(session_factory, service, bot, description="первое")
    second = await _create(session_factory, service, bot, description="второе")

    assert first.ticket.guid != second.ticket.guid


async def test_card_message_id_resolves_to_ticket(
    app_config: AppConfig, session_factory, clock
) -> None:
    """Ответ менеджера на карточку должен находить тикет — иначе reply не работает."""
    service = await _service(app_config, clock)
    bot = FakeBot()
    created = await _create(session_factory, service, bot)
    card = bot.sent[0]

    async with session_factory() as session:
        resolved = await service.resolve_ticket_id(
            session, chat_id=card.chat.id, message_id=card.message_id
        )

    assert resolved == created.ticket.id


async def test_reply_to_foreign_message_resolves_to_nothing(
    app_config: AppConfig, session_factory, clock
) -> None:
    service = await _service(app_config, clock)
    await _create(session_factory, service, FakeBot())

    async with session_factory() as session:
        resolved = await service.resolve_ticket_id(
            session, chat_id=-900000001, message_id=999999
        )

    assert resolved is None


async def test_take_assigns_manager_without_extra_client_message(
    app_config: AppConfig, session_factory, clock
) -> None:
    service = await _service(app_config, clock)
    bot = FakeBot()
    created = await _create(session_factory, service, bot)
    bot.sent.clear()

    async with session_factory() as session, session.begin():
        result = await service.take(
            session, bot, ticket_id=created.ticket.id, manager_id=MANAGER_ID
        )

    assert result.ok is True
    assert bot.sent == []
    async with session_factory() as session:
        ticket = await service.list_open(session)
    assert [t.manager_id for t in ticket] == [MANAGER_ID]


async def test_take_twice_by_other_manager_is_refused(
    app_config: AppConfig, session_factory, clock
) -> None:
    service = await _service(app_config, clock)
    created = await _create(session_factory, service, FakeBot())

    async with session_factory() as session, session.begin():
        await service.take(session, FakeBot(), ticket_id=created.ticket.id, manager_id=MANAGER_ID)
    async with session_factory() as session, session.begin():
        result = await service.take(
            session, FakeBot(), ticket_id=created.ticket.id, manager_id=OTHER_MANAGER_ID
        )

    assert result.ok is False
    assert result.reason is not None


async def test_take_missing_ticket_is_refused(
    app_config: AppConfig, session_factory, clock
) -> None:
    service = await _service(app_config, clock)
    async with session_factory() as session, session.begin():
        result = await service.take(session, FakeBot(), ticket_id=987654, manager_id=MANAGER_ID)
    assert result.ok is False


async def test_manager_reply_reaches_client(app_config: AppConfig, session_factory, clock) -> None:
    service = await _service(app_config, clock)
    bot = FakeBot()
    created = await _create(session_factory, service, bot)
    bot.sent.clear()

    async with session_factory() as session, session.begin():
        result = await service.relay_from_manager(
            session,
            bot,
            ticket_id=created.ticket.id,
            manager_id=MANAGER_ID,
            text="Проверьте кабель, пожалуйста",
        )

    assert result.ok is True
    assert bot.sent[0].chat.id == CLIENT_ID
    assert "Вам ответили по обращению" in bot.sent[0].text
    assert "Проверьте кабель, пожалуйста" in bot.sent[0].text
    async with session_factory() as session:
        history = await messages_repo.get_history(session, created.ticket.id)
    assert history[-1].from_user_id == MANAGER_ID


async def test_reply_by_new_ticket_assigns_responder(
    app_config: AppConfig, session_factory, clock
) -> None:
    """Ответ на новое обращение сам делает менеджера ведущим — без отдельного /take."""
    service = await _service(app_config, clock)
    created = await _create(session_factory, service, FakeBot())

    async with session_factory() as session, session.begin():
        await service.relay_from_manager(
            session,
            FakeBot(),
            ticket_id=created.ticket.id,
            manager_id=MANAGER_ID,
            text="Добрый день",
        )

    async with session_factory() as session:
        open_tickets = await service.list_open(session)
    assert open_tickets[0].state == TicketState.ASSIGNED.value
    assert open_tickets[0].manager_id == MANAGER_ID


async def test_reply_by_foreign_manager_is_refused(
    app_config: AppConfig, session_factory, clock
) -> None:
    service = await _service(app_config, clock)
    created = await _create(session_factory, service, FakeBot())
    async with session_factory() as session, session.begin():
        await service.take(session, FakeBot(), ticket_id=created.ticket.id, manager_id=MANAGER_ID)

    bot = FakeBot()
    async with session_factory() as session, session.begin():
        result = await service.relay_from_manager(
            session,
            bot,
            ticket_id=created.ticket.id,
            manager_id=OTHER_MANAGER_ID,
            text="Чужой ответ",
        )

    assert result.ok is False
    assert bot.to(CLIENT_ID) == []


async def test_manager_photo_reply_reaches_client_and_history(
    app_config: AppConfig, session_factory, clock
) -> None:
    service = await _service(app_config, clock)
    created = await _create(session_factory, service, FakeBot())
    bot = FakeBot()

    async with session_factory() as session, session.begin():
        result = await service.relay_from_manager(
            session,
            bot,
            ticket_id=created.ticket.id,
            manager_id=MANAGER_ID,
            text="Посмотрите на крепление",
            photo_file_id="manager-photo-id",
        )

    assert result.ok is True
    delivered = bot.photos_to(CLIENT_ID)
    assert len(delivered) == 1
    assert delivered[0].photo == "manager-photo-id"
    assert "Вам ответили по обращению" in (delivered[0].caption or "")
    assert "Посмотрите на крепление" in (delivered[0].caption or "")
    async with session_factory() as session:
        history = await messages_repo.get_history(session, created.ticket.id)
    assert history[-1].photo_file_id == "manager-photo-id"


async def test_client_message_goes_to_staff(app_config: AppConfig, session_factory, clock) -> None:
    service = await _service(app_config, clock)
    bot = FakeBot()
    created = await _create(session_factory, service, bot)
    bot.sent.clear()

    async with session_factory() as session, session.begin():
        result = await service.relay_from_client(
            session, bot, ticket_id=created.ticket.id, text="Ещё вопрос"
        )

    assert result.ok is True
    expected = {
        *app_config.settings.support.manager_user_ids,
        *app_config.settings.support.admin_user_ids,
    }
    assert {item.chat.id for item in bot.sent} == expected
    assert "Ещё вопрос" in bot.sent[0].text


async def test_client_follow_up_escapes_html_for_manager(
    app_config: AppConfig, session_factory, clock
) -> None:
    service = await _service(app_config, clock)
    bot = FakeBot()
    created = await _create(session_factory, service, bot)
    bot.sent.clear()

    async with session_factory() as session, session.begin():
        result = await service.relay_from_client(
            session,
            bot,
            ticket_id=created.ticket.id,
            text="<b>Дополнение</b> & <сломано>",
        )

    assert result.ok is True
    assert "&lt;b&gt;Дополнение&lt;/b&gt; &amp; &lt;сломано&gt;" in bot.sent[0].text
    assert "<сломано>" not in bot.sent[0].text


async def test_close_notifies_and_removes_from_queue(
    app_config: AppConfig, session_factory, clock
) -> None:
    service = await _service(app_config, clock)
    bot = FakeBot()
    created = await _create(session_factory, service, bot)
    async with session_factory() as session, session.begin():
        await service.take(session, bot, ticket_id=created.ticket.id, manager_id=MANAGER_ID)
    bot.sent.clear()

    async with session_factory() as session, session.begin():
        result = await service.close(
            session, bot, ticket_id=created.ticket.id, manager_id=MANAGER_ID
        )

    assert result.ok is True
    assert bot.sent == []
    async with session_factory() as session:
        assert await service.list_open(session) == []


async def test_close_by_foreign_manager_is_refused(
    app_config: AppConfig, session_factory, clock
) -> None:
    service = await _service(app_config, clock)
    created = await _create(session_factory, service, FakeBot())
    async with session_factory() as session, session.begin():
        await service.take(session, FakeBot(), ticket_id=created.ticket.id, manager_id=MANAGER_ID)

    async with session_factory() as session, session.begin():
        result = await service.close(
            session, FakeBot(), ticket_id=created.ticket.id, manager_id=OTHER_MANAGER_ID
        )
    assert result.ok is False


async def test_unassigned_ticket_may_be_closed(
    app_config: AppConfig, session_factory, clock
) -> None:
    """Закрыть спам-обращение можно, не беря его в работу.

    Схема это позволяет: constraint требует assigned_at только при закрытии
    ВЗЯТОГО тикета, а у нового его нет.
    """
    service = await _service(app_config, clock)
    created = await _create(session_factory, service, FakeBot())

    async with session_factory() as session, session.begin():
        result = await service.close(
            session, FakeBot(), ticket_id=created.ticket.id, manager_id=MANAGER_ID
        )
    assert result.ok is True


async def test_reply_to_closed_ticket_is_refused(
    app_config: AppConfig, session_factory, clock
) -> None:
    service = await _service(app_config, clock)
    created = await _create(session_factory, service, FakeBot())
    async with session_factory() as session, session.begin():
        await service.close(session, FakeBot(), ticket_id=created.ticket.id, manager_id=MANAGER_ID)

    async with session_factory() as session, session.begin():
        result = await service.relay_from_manager(
            session,
            FakeBot(),
            ticket_id=created.ticket.id,
            manager_id=MANAGER_ID,
            text="поздно",
        )
    assert result.ok is False


async def test_ticket_created_even_when_staff_unreachable(
    app_config: AppConfig, session_factory, clock
) -> None:
    """Недоступные сотрудники не должны приводить к потере обращения клиента."""
    service = await _service(app_config, clock)
    created = await _create(session_factory, service, FakeBot(fail_send=True))

    assert created.notified is False
    assert created.ticket.id > 0
    async with session_factory() as session:
        history = await messages_repo.get_history(session, created.ticket.id)
    assert len(history) == 1


async def test_single_open_ticket_is_found_for_reply(
    app_config: AppConfig, session_factory, clock
) -> None:
    service = await _service(app_config, clock)
    created = await _create(session_factory, service, FakeBot())

    async with session_factory() as session:
        found = await service.ticket_for_reply(session, telegram_user_id=CLIENT_ID)

    assert found is not None
    assert found.id == created.ticket.id


async def test_two_open_tickets_do_not_guess(app_config: AppConfig, session_factory, clock) -> None:
    """Два открытых обращения — привязки нет: пусть клиент начнёт новое /start."""
    service = await _service(app_config, clock)
    bot = FakeBot()
    await _create(session_factory, service, bot, description="первое")
    await _create(session_factory, service, bot, description="второе")

    async with session_factory() as session:
        found = await service.ticket_for_reply(session, telegram_user_id=CLIENT_ID)

    assert found is None


async def test_manager_check(app_config: AppConfig, session_factory, clock) -> None:
    service = await _service(app_config, clock)
    assert service.is_manager(MANAGER_ID) is True
    assert service.is_manager(USER_ID) is False


async def test_long_photo_card_keeps_full_text_and_routes_both_messages(
    app_config: AppConfig, session_factory, clock
) -> None:
    service = TicketService(app_config, clock)
    bot = FakeBot()
    description = "Подробности & <ошибка> " * 60
    async with session_factory() as session, session.begin():
        created = await service.create_ticket(
            session,
            bot,
            telegram_user_id=CLIENT_ID,
            telegram_username="anya",
            category_key="defect",
            description=description,
            photo_file_id="photo-id",
        )
    assert created.notified
    assert len(bot.photos_to(MANAGER_ID)[0].caption or "") <= 1024
    assert "Подробности &amp; &lt;ошибка&gt; " * 60 in "".join(bot.texts_to(MANAGER_ID))
    async with session_factory() as session:
        for sent in bot.to(MANAGER_ID):
            assert (
                await service.resolve_ticket_id(
                    session, chat_id=sent.chat.id, message_id=sent.message_id
                )
                == created.ticket.id
            )


async def test_long_manager_photo_reply_preserves_text_and_reply_routes(
    app_config: AppConfig, session_factory, clock
) -> None:
    service = TicketService(app_config, clock)
    created = await _create(session_factory, service, FakeBot())
    bot = FakeBot()
    text = "Ответ & <подробности> " * 60
    async with session_factory() as session, session.begin():
        result = await service.relay_from_manager(
            session,
            bot,
            ticket_id=created.ticket.id,
            manager_id=MANAGER_ID,
            text=text,
            photo_file_id="reply-photo",
        )
    assert result.ok
    assert len(bot.photos_to(CLIENT_ID)[0].caption or "") <= 1024
    assert "Ответ &amp; &lt;подробности&gt; " * 60 in "".join(bot.texts_to(CLIENT_ID))
    async with session_factory() as session:
        for sent in bot.to(CLIENT_ID):
            assert (
                await service.resolve_ticket_id(
                    session, chat_id=sent.chat.id, message_id=sent.message_id
                )
                == created.ticket.id
            )


async def test_closing_new_ticket_preserves_personal_history(
    app_config: AppConfig, session_factory, clock
) -> None:
    service = TicketService(app_config, clock)
    created = await _create(session_factory, service, FakeBot())
    async with session_factory() as session, session.begin():
        result = await service.close(
            session, FakeBot(), ticket_id=created.ticket.id, manager_id=MANAGER_ID
        )
    assert result.ok
    async with session_factory() as session:
        history = await tickets_repo.list_closed_for_staff(session, MANAGER_ID)
        assert [ticket.id for ticket in history] == [created.ticket.id]
        assert await tickets_repo.list_closed_for_staff(session, OTHER_MANAGER_ID) == []


async def test_client_cannot_append_to_closed_ticket(
    app_config: AppConfig, session_factory, clock
) -> None:
    service = TicketService(app_config, clock)
    bot = FakeBot()
    created = await _create(session_factory, service, bot)
    async with session_factory() as session, session.begin():
        await service.close(session, FakeBot(), ticket_id=created.ticket.id, manager_id=MANAGER_ID)
    bot.sent.clear()
    async with session_factory() as session, session.begin():
        result = await service.relay_from_client(
            session, bot, ticket_id=created.ticket.id, text="Позднее сообщение"
        )
    assert not result.ok
    assert bot.sent == []
    async with session_factory() as session:
        assert len(await messages_repo.get_history(session, created.ticket.id)) == 1


async def test_legacy_closed_ticket_without_assignment_stays_visible_to_closer(
    app_config: AppConfig, session_factory, clock
) -> None:
    service = TicketService(app_config, clock)
    created = await _create(session_factory, service, FakeBot())
    async with session_factory() as session, session.begin():
        await session.execute(
            update(Ticket)
            .where(Ticket.id == created.ticket.id)
            .values(state="closed", manager_id=MANAGER_ID, closed_at=clock.now())
        )
    async with session_factory() as session:
        history = await tickets_repo.list_closed_for_staff(session, MANAGER_ID)
        assert [ticket.id for ticket in history] == [created.ticket.id]
        assert await tickets_repo.was_assigned_to(
            session, ticket_id=created.ticket.id, telegram_user_id=MANAGER_ID
        )
