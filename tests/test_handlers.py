"""Хендлеры: клиентский сценарий и менеджерские команды, с настоящим FSM.

Хендлеры вызываются напрямую (у них обычная сигнатура), а не через
`Dispatcher.feed_update`: так тест проверяет логику обработчика, а не
маршрутизацию. Маршрутизация — предмет tests/test_wiring.py.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from aiogram import Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import User

from app.bot.handlers import manager_commands, user_flow
from app.config import AppConfig
from app.repositories import tickets as tickets_repo
from app.services.tickets import TicketService
from tests.factories import CHAT_ID, MANAGER_ID, FakeBot, manager_message, private_message

CLIENT_ID = 4242


def _fsm(chat_id: int, user_id: int) -> FSMContext:
    storage = MemoryStorage()
    key = StorageKey(bot_id=1, chat_id=chat_id, user_id=user_id)
    return FSMContext(storage=storage, key=key)


def _bound(message: Any, bot: FakeBot) -> Any:
    return message.as_(cast(Bot, bot))


# --- Клиентский сценарий ------------------------------------------------------


async def test_start_shows_categories(app_config: AppConfig, clock, session_factory) -> None:
    bot = FakeBot()
    service = TicketService(app_config, clock)
    state = _fsm(CLIENT_ID, CLIENT_ID)
    message = _bound(private_message("/start", user_id=CLIENT_ID), bot)

    await user_flow.handle_start(message, state, app_config, service)

    assert len(bot.sent) == 1
    assert bot.buttons(bot.sent[0]) == [
        "📚 Инструкции к товарам",
        "🛠 Техническая поддержка",
    ]
    assert await state.get_state() is None


async def test_start_with_category_payload_skips_choice(
    app_config: AppConfig, clock, session_factory
) -> None:
    """QR со `?start=defect` — категория выбрана сразу, клиент пишет описание."""
    bot = FakeBot()
    service = TicketService(app_config, clock)
    state = _fsm(CLIENT_ID, CLIENT_ID)
    message = _bound(private_message("/start defect", user_id=CLIENT_ID), bot)

    await user_flow.handle_start(message, state, app_config, service)

    assert await state.get_state() == user_flow.TicketFlow.waiting_description.state
    assert (await state.get_data())["category"] == "defect"


async def test_start_with_unknown_payload_falls_back_to_choice(
    app_config: AppConfig, clock, session_factory
) -> None:
    bot = FakeBot()
    service = TicketService(app_config, clock)
    state = _fsm(CLIENT_ID, CLIENT_ID)
    message = _bound(private_message("/start promo2026", user_id=CLIENT_ID), bot)

    await user_flow.handle_start(message, state, app_config, service)

    assert await state.get_state() is None


async def test_start_by_manager_shows_hint(app_config: AppConfig, clock, session_factory) -> None:
    """Менеджер в личке не должен создавать тикет сам себе."""
    bot = FakeBot()
    service = TicketService(app_config, clock)
    state = _fsm(MANAGER_ID, MANAGER_ID)
    message = _bound(private_message("/start", user_id=MANAGER_ID), bot)

    await user_flow.handle_start(message, state, app_config, service)

    assert "кабинет" in bot.texts_to(MANAGER_ID)[0]


async def test_description_creates_ticket(app_config: AppConfig, clock, session_factory) -> None:
    bot = FakeBot()
    service = TicketService(app_config, clock)
    state = _fsm(CLIENT_ID, CLIENT_ID)
    await state.set_state(user_flow.TicketFlow.waiting_description)
    await state.update_data(category="defect")
    message = _bound(private_message("Не включается", user_id=CLIENT_ID), bot)

    async with session_factory() as session:
        await user_flow.handle_description(
            message, state, app_config, service, session, cast(Bot, bot)
        )

    client_texts = bot.texts_to(CLIENT_ID)
    assert len(client_texts) == 1 and "принято" in client_texts[0]
    # Карточка ушла менеджерам, клиенту — только подтверждение.
    assert len(bot.to(MANAGER_ID)) == 1
    assert await state.get_state() is None
    async with session_factory() as session:
        open_tickets = await tickets_repo.list_open_tickets(session)
    assert len(open_tickets) == 1
    assert open_tickets[0].description == "Не включается"


async def test_too_long_description_is_refused_and_state_kept(
    app_config: AppConfig, clock, session_factory
) -> None:
    """Слишком длинный текст — отказ без потери выбранной категории."""
    bot = FakeBot()
    service = TicketService(app_config, clock)
    state = _fsm(CLIENT_ID, CLIENT_ID)
    await state.set_state(user_flow.TicketFlow.waiting_description)
    await state.update_data(category="defect")
    long_text = "я" * (app_config.settings.support.max_message_chars + 1)
    message = _bound(private_message(long_text, user_id=CLIENT_ID), bot)

    async with session_factory() as session:
        await user_flow.handle_description(
            message, state, app_config, service, session, cast(Bot, bot)
        )

    assert "слишком длинное" in bot.texts_to(CLIENT_ID)[0]
    assert await state.get_state() == user_flow.TicketFlow.waiting_description.state
    async with session_factory() as session:
        assert await tickets_repo.list_open_tickets(session) == []


async def test_plain_message_requires_opening_a_ticket_chat(
    app_config: AppConfig, clock, session_factory
) -> None:
    bot = FakeBot()
    service = TicketService(app_config, clock)
    async with session_factory() as session, session.begin():
        await service.create_ticket(
            session,
            cast(Bot, bot),
            telegram_user_id=CLIENT_ID,
            telegram_username="anya",
            category_key="defect",
            description="первое",
        )
    bot.sent.clear()

    message = _bound(private_message("ещё деталь", user_id=CLIENT_ID), bot)
    await user_flow.handle_client_message(message, service)

    assert not bot.texts_to(MANAGER_ID)
    assert "Мои обращения" in bot.texts_to(CLIENT_ID)[0]


async def test_plain_message_without_ticket_asks_for_start(
    app_config: AppConfig, clock, session_factory
) -> None:
    bot = FakeBot()
    service = TicketService(app_config, clock)
    message = _bound(private_message("привет", user_id=CLIENT_ID), bot)

    await user_flow.handle_client_message(message, service)

    assert "Мои обращения" in bot.texts_to(CLIENT_ID)[0]


# --- Менеджерские команды -----------------------------------------------------


class _StubQuery:
    """Нажатие кнопки: хендлеру от CallbackQuery нужны `from_user` и `answer`.

    Настоящий `CallbackQuery` тянет за собой `bot`, метод отправки и разбор
    подделки — всё это проверяется в tests/test_wiring.py. Здесь — только то,
    что читает хендлер.
    """

    from_user = User(id=555, is_bot=False, first_name="Менеджер")

    def __init__(self) -> None:
        self.answered: list[str] = []

    async def answer(self, text: str = "", show_alert: bool = False) -> None:
        self.answered.append(text)


async def _ticket(session_factory, app_config, clock, bot, *, description: str = "проблема"):
    service = TicketService(app_config, clock)
    async with session_factory() as session, session.begin():
        created = await service.create_ticket(
            session,
            cast(Bot, bot),
            telegram_user_id=CLIENT_ID,
            telegram_username="anya",
            category_key="defect",
            description=description,
        )
    return created.ticket


async def test_queue_lists_open_tickets(app_config: AppConfig, clock, session_factory) -> None:
    bot = FakeBot()
    await _ticket(session_factory, app_config, clock, bot)
    service = TicketService(app_config, clock)
    message = _bound(manager_message("/queue"), bot)
    bot.sent.clear()

    async with session_factory() as session:
        await manager_commands.handle_queue(message, session, service, app_config)

    assert "№1" in bot.texts_to(CHAT_ID)[0]


async def test_queue_empty(app_config: AppConfig, clock, session_factory) -> None:
    bot = FakeBot()
    service = TicketService(app_config, clock)
    message = _bound(manager_message("/queue"), bot)

    async with session_factory() as session:
        await manager_commands.handle_queue(message, session, service, app_config)

    assert "пуста" in bot.texts_to(CHAT_ID)[0]


async def test_take_by_command(app_config: AppConfig, clock, session_factory) -> None:
    from aiogram.filters import CommandObject

    bot = FakeBot()
    ticket = await _ticket(session_factory, app_config, clock, bot)
    service = TicketService(app_config, clock)
    message = _bound(manager_message("/take 1"), bot)
    bot.sent.clear()

    async with session_factory() as session:
        await manager_commands.handle_take(
            message, CommandObject(command="take", args="1"), cast(Bot, bot), session, service
        )

    # Клиентский экран обновляется при следующем открытии: лишнее сообщение
    # в личке не создаётся. Менеджеру остаётся подтверждение команды.
    assert not bot.texts_to(CLIENT_ID)
    assert any("готово" in t for t in bot.texts_to(CHAT_ID))
    async with session_factory() as session:
        stored = await tickets_repo.get_ticket(session, ticket.id)
    assert stored is not None and stored.manager_id == 555


@pytest.mark.parametrize("args", [None, "", "abc", "0", "-1"])
async def test_take_without_valid_id_shows_format(
    app_config: AppConfig, clock, session_factory, args: str | None
) -> None:
    from aiogram.filters import CommandObject

    bot = FakeBot()
    service = TicketService(app_config, clock)
    message = _bound(manager_message("/take"), bot)

    async with session_factory() as session:
        await manager_commands.handle_take(
            message, CommandObject(command="take", args=args), cast(Bot, bot), session, service
        )

    assert "Формат" in bot.texts_to(CHAT_ID)[0]


async def test_manager_reply_reaches_client(app_config: AppConfig, clock, session_factory) -> None:
    bot = FakeBot()
    ticket = await _ticket(session_factory, app_config, clock, bot)
    card = bot.to(MANAGER_ID)[0]  # карточка тикета у выбранного менеджера
    assert card.chat.id == MANAGER_ID

    card_message = private_message("карточка", user_id=MANAGER_ID, message_id=card.message_id)
    reply = _bound(
        private_message("Добрый день", user_id=MANAGER_ID, reply_to_message=card_message), bot
    )
    service = TicketService(app_config, clock)
    bot.sent.clear()

    async with session_factory() as session:
        await manager_commands.handle_manager_reply(reply, cast(Bot, bot), session, service)

    assert any("Добрый день" in text for text in bot.texts_to(CLIENT_ID))
    assert any(f"№{ticket.id}" in t for t in bot.texts_to(MANAGER_ID))


async def test_reply_to_unknown_message_explains(
    app_config: AppConfig, clock, session_factory
) -> None:
    bot = FakeBot()
    await _ticket(session_factory, app_config, clock, bot)
    service = TicketService(app_config, clock)
    foreign = manager_message("чужое", message_id=987654)
    reply = _bound(manager_message("ответ", reply_to=foreign), bot)
    bot.sent.clear()

    async with session_factory() as session:
        await manager_commands.handle_manager_reply(reply, cast(Bot, bot), session, service)

    assert "Не понял" in bot.texts_to(CHAT_ID)[0]


async def test_close_by_button(app_config: AppConfig, clock, session_factory) -> None:
    bot = FakeBot()
    ticket = await _ticket(session_factory, app_config, clock, bot)
    service = TicketService(app_config, clock)
    callback = user_flow.callbacks.CloseTicket(ticket.id)

    query = _StubQuery()
    async with session_factory() as session:
        await manager_commands.handle_close_button(
            cast(Any, query), callback, cast(Bot, bot), session, service
        )

    assert query.answered and "готово" in query.answered[0]
    async with session_factory() as session:
        stored = await tickets_repo.get_ticket(session, ticket.id)
    assert stored is not None and stored.closed_at is not None


# --- Не-текст: тишина вместо ответа тут дороже лишнего сообщения ---------------


def _photo(user_id: int) -> Any:
    return private_message(
        None,
        user_id=user_id,
        photo=[{"file_id": "p", "file_unique_id": "pu", "width": 90, "height": 90}],
    )


async def test_photo_while_choosing_category_asks_for_button() -> None:
    bot = FakeBot()
    state = _fsm(CLIENT_ID, CLIENT_ID)
    await state.set_state(user_flow.TicketFlow.waiting_category)

    await user_flow.handle_wrong_in_category(_bound(_photo(CLIENT_ID), bot))

    assert "кнопкой" in bot.texts_to(CLIENT_ID)[0]


async def test_photo_while_describing_asks_for_text(app_config: AppConfig, session_factory) -> None:
    bot = FakeBot()
    await user_flow.handle_non_text_description(_bound(_photo(CLIENT_ID), bot))
    assert "текстом" in bot.texts_to(CLIENT_ID)[0]


async def test_photo_with_open_ticket_is_not_forwarded(
    app_config: AppConfig, clock, session_factory
) -> None:
    """Фото не уходит менеджеру, но клиент узнаёт об этом, а не молчит."""
    from app.services.tickets import TicketService

    bot = FakeBot()
    service = TicketService(app_config, clock)
    await _ticket(session_factory, app_config, clock, bot)
    bot.sent.clear()

    await user_flow.handle_client_non_text(_bound(_photo(CLIENT_ID), bot), service)

    assert len(bot.to(CHAT_ID)) == 0
    assert "текстом" in bot.texts_to(CLIENT_ID)[0]
