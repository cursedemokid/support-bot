"""Сборка роутеров: апдейт находит ровно одного адресата.

Маршрутизация — то, что нельзя проверить, вызвав хендлер напрямую. Здесь
апдейт идёт через настоящий конвейер `feed_update`, вместе с middleware, а
ответы перехватывает `FakeBot`.

Диспетчер собирается ОДИН раз на модуль: роутеры aiogram — объекты уровня
модуля, и `include_router` привязывает их к родителю навсегда. Это идиома
самого aiogram, а не наша особенность (см. шапку app/bot/handlers/__init__.py).

Главное свойство, которое здесь стережётся: catch-all клиентского сценария
(`F.text`) стоит последним. Всё, что встанет за ним, не получит ни одного
текстового апдейта — а выглядит это не сбоем, а тишиной.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import re
import zoneinfo
from collections.abc import AsyncIterator
from typing import Any, cast

import pytest
from aiogram import Bot, Dispatcher
from aiogram.types import Chat, Message, Update, User

from app.bot.factory import build_dispatcher, resolve_allowed_updates
from app.clock import ManualClock
from app.config import AppConfig, load_secrets, load_settings
from app.db.engine import build_engine
from app.db.models import SCHEMA
from app.db.session import build_session_factory
from app.migrate import migrate
from app.repositories import tickets as tickets_repo
from tests.conftest import REFERENCE_SETTINGS, REPO_ROOT
from tests.factories import CHAT_ID, MANAGER_ID, USER_ID, FakeBot, manager_message, private_message

CLIENT_ID = 4242
# У каждого теста свой клиент: FSM живёт в общем на модуль диспетчере, и один
# и тот же id протащил бы состояние предыдущего теста в следующий.
STRANGER_ID = 5001


def _test_clock() -> ManualClock:
    return ManualClock(
        dt.datetime(2026, 5, 1, 12, 0, tzinfo=zoneinfo.ZoneInfo("UTC")),
        zoneinfo.ZoneInfo("UTC"),
    )


def _update(message: Message) -> Update:
    return Update(update_id=1, message=message)


@pytest.fixture(scope="module")
def database_url() -> str:
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL не задан — тесты маршрутизации пропущены")
    return url


@pytest.fixture(scope="module")
async def dispatcher_and_factory(
    database_url: str,
) -> AsyncIterator[tuple[Dispatcher, Any, AppConfig]]:
    """Одна сборка диспетчера на модуль + своя фабрика сессий.

    Не общие фикстуры из conftest: те со `scope="function"`, а диспетчер
    собирается один раз — иначе второй `include_router` падает.
    """
    await migrate(database_url, REPO_ROOT / "migrations")
    engine = build_engine(database_url)
    session_factory = build_session_factory(engine)
    config = AppConfig(
        load_settings(REFERENCE_SETTINGS),
        load_secrets(),
        path=None,
    )
    clock = ManualClock(
        __import__("datetime").datetime(
            2026, 5, 1, 12, 0, tzinfo=__import__("zoneinfo").ZoneInfo("UTC")
        ),
        __import__("zoneinfo").ZoneInfo("UTC"),
    )
    try:
        yield build_dispatcher(config, clock, session_factory), session_factory, config
    finally:
        await engine.dispose()


@pytest.fixture(autouse=True)
async def clean_tables(database_url: str) -> AsyncIterator[None]:
    """Пустые таблицы перед каждым тестом модуля.

    Диспетчер здесь один на весь модуль, но данные — нет: без этой чистки
    тикет из предыдущего теста приезжает в очередь следующего, а id сообщений
    FakeBot начинаются заново и попадают в занятые строки message_routes.
    """
    import asyncpg

    conn = await asyncpg.connect(database_url.replace("postgresql+asyncpg://", "postgresql://"))
    try:
        await conn.execute(
            "truncate support.message_routes, support.message_history, "
            "support.tickets restart identity cascade"
        )
        yield
    finally:
        await conn.close()


async def _feed(dispatcher: Dispatcher, bot: FakeBot, message: Message) -> None:
    await dispatcher.feed_update(cast(Bot, bot), _update(message))


async def test_start_reaches_client_scenario(dispatcher_and_factory) -> None:
    dispatcher, _, _ = dispatcher_and_factory
    bot = FakeBot()
    await _feed(dispatcher, bot, private_message("/start", user_id=CLIENT_ID))
    assert bot.texts_to(CLIENT_ID)


async def test_manager_command_not_answered_to_client(dispatcher_and_factory) -> None:
    """Клиент, набравший /queue, ответа менеджера не получает.

    До catch-all сценария команда доходит как обычный текст, и там её
    встречает «начните с /start» — не молчание.
    """
    dispatcher, _, _ = dispatcher_and_factory
    bot = FakeBot()
    await _feed(dispatcher, bot, private_message("/queue", user_id=STRANGER_ID))
    assert "Очередь" not in " ".join(bot.texts)
    assert "/start" in " ".join(bot.texts)


async def test_manager_in_chat_gets_queue(dispatcher_and_factory) -> None:
    dispatcher, _, _ = dispatcher_and_factory
    bot = FakeBot()
    await _feed(dispatcher, bot, manager_message("/queue"))
    assert "пуста" in " ".join(bot.texts_to(CHAT_ID))


async def test_private_manager_is_not_client(dispatcher_and_factory) -> None:
    """Менеджер в личке получает подсказку, а не форму обращения."""
    dispatcher, _, _ = dispatcher_and_factory
    bot = FakeBot()
    await _feed(dispatcher, bot, private_message("/start", user_id=MANAGER_ID))
    assert "кабинет" in " ".join(bot.texts_to(MANAGER_ID))


async def test_unrelated_user_id_is_just_a_client(dispatcher_and_factory) -> None:
    dispatcher, _, _ = dispatcher_and_factory
    bot = FakeBot()
    await _feed(dispatcher, bot, private_message("/queue", user_id=USER_ID))
    assert "/start" in " ".join(bot.texts)


async def test_callback_buttons_take_ticket(dispatcher_and_factory) -> None:
    """Кнопка «Взять» под карточкой должна вести в менеджерский роутер."""
    from app.bot.keyboards import ticket_keyboard
    from app.services.tickets import TicketService

    dispatcher, session_factory, config = dispatcher_and_factory
    bot = FakeBot()
    service = TicketService(config, _test_clock())
    async with session_factory() as session, session.begin():
        created = await service.create_ticket(
            session,
            cast(Bot, bot),
            telegram_user_id=CLIENT_ID,
            telegram_username="anya",
            category_key="defect",
            description="проблема",
        )
    card = bot.sent[0]
    button = ticket_keyboard(created.ticket.id, taken=False).inline_keyboard[0][0]
    assert button.callback_data is not None

    callback = Update(
        update_id=2,
        callback_query={
            "id": "cb-1",
            "from": User(id=MANAGER_ID, is_bot=False, first_name="Менеджер").model_dump(),
            "chat_instance": "ci",
            "data": button.callback_data,
            "message": {
                "message_id": card.message_id,
                "date": 3738021089,
                "chat": Chat(id=CHAT_ID, type="supergroup", title="Поддержка").model_dump(),
                "from": {"id": 1, "is_bot": True, "first_name": "bot"},
                "text": card.text,
            },
        },
    )
    await dispatcher.feed_update(cast(Bot, bot), callback)

    async with session_factory() as session:
        stored = await tickets_repo.get_ticket(session, created.ticket.id)
    assert stored is not None and stored.manager_id == MANAGER_ID


async def test_client_message_creates_ticket_end_to_end(dispatcher_and_factory) -> None:
    """Полный клиентский путь через конвейер: категория → описание → тикет.

    FSM живёт в диспетчере, поэтому шаги идут двумя апдейтами подряд — ровно
    так, как это делает живой клиент.
    """
    dispatcher, _, _ = dispatcher_and_factory
    bot = FakeBot()
    await _feed(dispatcher, bot, private_message("/start defect", user_id=CLIENT_ID + 1))
    await _feed(dispatcher, bot, private_message("не работает", user_id=CLIENT_ID + 1))
    assert any("принято" in t for t in bot.texts_to(CLIENT_ID + 1))


async def test_full_journey_client_to_manager_and_back(dispatcher_and_factory) -> None:
    """Сквозной путь одной прогонкой: заявка → взятие кнопкой → ответ → закрытие.

    По отдельности все шаги проверены выше и в test_handlers.py, но только
    здесь клиентский FSM, менеджерская кнопка и маршрутизация ответа работают
    в одном конвейере. Поломка вида «роутер менеджеров встал после catch-all»
    видна только тут: остальные тесты её не заметят.
    """

    dispatcher, session_factory, _ = dispatcher_and_factory
    client_id = CLIENT_ID + 100
    bot = FakeBot()

    # 1. Клиент открывает бота по QR с категорией и пишет суть.
    await _feed(dispatcher, bot, private_message("/start defect", user_id=client_id))
    await _feed(dispatcher, bot, private_message("экран не горит", user_id=client_id))
    confirmation = bot.texts_to(client_id)[-1]
    assert "принято" in confirmation
    # Подтверждение приходит с HTML-разметкой (`<b>№1</b>`) — id берём цифрами.
    match = re.search(r"№(\d+)", confirmation)
    assert match is not None
    ticket_id = int(match.group(1))

    # 2. Менеджер берёт тикет кнопкой под карточкой.
    # Кнопки берём из самой карточки, а не собираем заново: иначе тест
    # проверял бы клавиатуру, собранную им же, а не то, что прислал бот.
    card = bot.to(MANAGER_ID)[-1]
    assert card.reply_markup is not None, "карточка ушла без кнопок"
    take_button = card.reply_markup.inline_keyboard[0][0]
    assert take_button.callback_data is not None
    await _click(dispatcher, bot, card.message_id, card.text, take_button.callback_data, MANAGER_ID)
    async with session_factory() as session:
        assigned = await tickets_repo.get_ticket(session, ticket_id)
    assert assigned is not None and assigned.manager_id == MANAGER_ID

    # 3. В режиме ответа менеджер продолжает видеть историю обращения.
    await _click(
        dispatcher,
        bot,
        card.message_id,
        card.text,
        f"mgrtkt:{ticket_id}",
        MANAGER_ID,
    )
    manager_screen = bot.to(MANAGER_ID)[-1]
    assert manager_screen.reply_markup is not None
    write_button = next(
        button
        for row in manager_screen.reply_markup.inline_keyboard
        for button in row
        if button.callback_data == "mgrchat:write"
    )
    manager_before_write = len(bot.to(MANAGER_ID))
    await _click(
        dispatcher,
        bot,
        manager_screen.message_id,
        getattr(manager_screen, "text", ""),
        write_button.callback_data,
        MANAGER_ID,
    )
    manager_write_screen = bot.to(MANAGER_ID)[manager_before_write:]
    assert any("экран не горит" in getattr(item, "text", "") for item in manager_write_screen)
    assert any("Напишите ответ" in getattr(item, "text", "") for item in manager_write_screen)

    await _feed(dispatcher, bot, private_message("Проверьте кабель", user_id=MANAGER_ID))
    assert "Проверьте кабель" in bot.texts_to(client_id)[-1]

    # 4. Клиент открывает нужный чат, включает режим ответа и дописывает.
    client_screen = bot.to(client_id)[-1]
    await _click(
        dispatcher,
        bot,
        client_screen.message_id,
        getattr(client_screen, "text", ""),
        "sup:tickets",
        client_id,
    )
    client_screen = bot.to(client_id)[-1]
    client_before_write = len(bot.to(client_id))
    await _click(
        dispatcher,
        bot,
        client_screen.message_id,
        getattr(client_screen, "text", ""),
        f"tkt:{ticket_id}",
        client_id,
    )
    client_screen = bot.to(client_id)[-1]
    await _click(
        dispatcher,
        bot,
        client_screen.message_id,
        getattr(client_screen, "text", ""),
        "chat:write",
        client_id,
    )
    client_write_screen = bot.to(client_id)[client_before_write:]
    assert any("экран не горит" in getattr(item, "text", "") for item in client_write_screen)
    assert any("Проверьте кабель" in getattr(item, "text", "") for item in client_write_screen)
    assert any("Напишите сообщение" in getattr(item, "text", "") for item in client_write_screen)
    await _feed(dispatcher, bot, private_message("не помогло", user_id=client_id))
    assert any("не помогло" in t for t in bot.texts_to(MANAGER_ID))

    # 5. Менеджер закрывает обращение.
    await _feed(dispatcher, bot, private_message(f"/close {ticket_id}", user_id=MANAGER_ID))
    assert "Проверьте кабель" in bot.texts_to(client_id)[-1]

    async with session_factory() as session:
        stored = await tickets_repo.get_ticket(session, ticket_id)
    assert stored is not None
    assert stored.state == "closed"
    assert stored.manager_id == MANAGER_ID


async def _click(
    dispatcher: Dispatcher,
    bot: FakeBot,
    message_id: int,
    text: str,
    data: str | None,
    user_id: int,
) -> None:
    """Нажатие inline-кнопки в чате менеджеров."""
    callback = Update(
        update_id=99,
        callback_query={
            "id": f"cb-{message_id}",
            "from": User(id=user_id, is_bot=False, first_name="Менеджер").model_dump(),
            "chat_instance": "ci",
            "data": data,
            "message": {
                "message_id": message_id,
                "date": 3738021089,
                "chat": {"id": user_id, "type": "private", "first_name": "Менеджер"},
                "from": {"id": 1, "is_bot": True, "first_name": "bot"},
                "text": text,
            },
        },
    )
    await dispatcher.feed_update(cast(Bot, bot), callback)


def test_allowed_updates_cover_messages_and_callbacks(dispatcher_and_factory) -> None:
    dispatcher, _, _ = dispatcher_and_factory
    updates = resolve_allowed_updates(dispatcher)
    assert "message" in updates
    assert "callback_query" in updates


async def test_concurrent_ticket_selections_send_pending_message_only_once(
    dispatcher_and_factory,
) -> None:
    from app.bot.handlers.user_flow import ClientFlow
    from app.repositories import messages as messages_repo
    from app.services.tickets import TicketService

    dispatcher, session_factory, config = dispatcher_and_factory
    bot = FakeBot()
    client_id = CLIENT_ID + 200
    service = TicketService(config, _test_clock())
    async with session_factory() as session, session.begin():
        first = await service.create_ticket(
            session,
            cast(Bot, bot),
            telegram_user_id=client_id,
            telegram_username="client",
            category_key="defect",
            description="Первое обращение",
        )
        second = await service.create_ticket(
            session,
            cast(Bot, bot),
            telegram_user_id=client_id,
            telegram_username="client",
            category_key="other",
            description="Второе обращение",
        )
    state = dispatcher.fsm.get_context(bot=cast(Bot, bot), chat_id=client_id, user_id=client_id)
    await state.set_state(ClientFlow.choosing_ticket)
    await state.update_data(
        pending_client_message={"text": "Только один раз", "photo_file_id": None}
    )
    await asyncio.gather(
        _click(dispatcher, bot, 2001, "Выберите", f"clientticket:{first.ticket.id}", client_id),
        _click(dispatcher, bot, 2002, "Выберите", f"clientticket:{second.ticket.id}", client_id),
    )
    async with session_factory() as session:
        history = [
            *await messages_repo.get_history(session, first.ticket.id),
            *await messages_repo.get_history(session, second.ticket.id),
        ]
        assert [item.message_text for item in history].count("Только один раз") == 1


def test_schema_is_support() -> None:
    assert SCHEMA == "support"
