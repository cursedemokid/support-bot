"""Regressions for client messages sent outside the guided chat flow."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message

from app.bot.handlers import user_flow
from app.config import AppConfig
from app.repositories import content as content_repo
from app.repositories import staff as staff_repo
from app.repositories import tickets as tickets_repo
from app.repositories import ui as ui_repo
from tests.factories import FakeBot, SentMessage, private_message

CLIENT_ID = 4242


def _fsm(chat_id: int, user_id: int) -> FSMContext:
    storage = MemoryStorage()
    key = StorageKey(bot_id=1, chat_id=chat_id, user_id=user_id)
    return FSMContext(storage=storage, key=key)


def _bound(message: Message, bot: FakeBot) -> Any:
    return message.as_(cast(Bot, bot))


@pytest.mark.parametrize("kind", ["text", "photo"])
async def test_unprompted_message_routes_to_the_only_open_ticket(
    kind: str, app_config: AppConfig, clock: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot = FakeBot()
    service = SimpleNamespace(
        is_manager=lambda _id: False,
        is_admin=lambda _id: False,
        is_staff=AsyncMock(return_value=False),
        ticket_for_reply=AsyncMock(return_value=SimpleNamespace(id=17, state="new")),
        relay_from_client=AsyncMock(return_value=SimpleNamespace(ok=True)),
    )
    session = SimpleNamespace(commit=AsyncMock())
    state = _fsm(CLIENT_ID, CLIENT_ID)
    monkeypatch.setattr(
        tickets_repo,
        "get_ticket",
        AsyncMock(return_value=SimpleNamespace(id=17, telegram_user_id=CLIENT_ID, state="new")),
    )
    monkeypatch.setattr(user_flow, "_render_ticket", AsyncMock(return_value=True))
    message = private_message(
        "дополнение" if kind == "text" else None,
        user_id=CLIENT_ID,
        photo=(
            [{"file_id": "photo-1", "file_unique_id": "unique", "width": 90, "height": 90}]
            if kind == "photo"
            else None
        ),
    )

    await user_flow.handle_client_message(
        _bound(message, bot), service, state=state, session=session, bot=cast(Bot, bot), clock=clock
    )

    service.relay_from_client.assert_awaited_once()
    call = service.relay_from_client.await_args.kwargs
    assert call["ticket_id"] == 17
    assert call["text"] == ("дополнение" if kind == "text" else "📷 Фото")
    assert call["photo_file_id"] == ("photo-1" if kind == "photo" else None)


@pytest.mark.parametrize(("text", "photo_id"), [("дополнение", None), ("подпись", "photo-1")])
async def test_unprompted_message_is_saved_until_client_selects_one_of_multiple_tickets(
    text: str,
    photo_id: str | None,
    app_config: AppConfig,
    clock: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = FakeBot()
    tickets = [
        SimpleNamespace(id=17, title="Заказ", description="Первое", state="new"),
        SimpleNamespace(id=18, title="Доставка", description="Второе", state="assigned"),
    ]
    monkeypatch.setattr(tickets_repo, "list_open_for_user", AsyncMock(return_value=tickets))
    monkeypatch.setattr(ui_repo, "list_messages", AsyncMock(return_value=[]))
    monkeypatch.setattr(ui_repo, "forget_all", AsyncMock())
    monkeypatch.setattr(ui_repo, "remember", AsyncMock())
    service = SimpleNamespace(
        is_manager=lambda _id: False,
        is_admin=lambda _id: False,
        is_staff=AsyncMock(return_value=False),
        ticket_for_reply=AsyncMock(return_value=None),
    )
    state = _fsm(CLIENT_ID, CLIENT_ID)
    bot_message = _bound(
        private_message(
            text if photo_id is None else None,
            user_id=CLIENT_ID,
            caption=text if photo_id is not None else None,
            photo=(
                [{"file_id": photo_id, "file_unique_id": "unique", "width": 90, "height": 90}]
                if photo_id is not None
                else None
            ),
        ),
        bot,
    )

    await user_flow.handle_client_message(
        bot_message,
        service,
        state=state,
        session=SimpleNamespace(commit=AsyncMock()),
        bot=cast(Bot, bot),
        clock=clock,
    )

    assert (await state.get_data())["pending_client_message"] == {
        "text": text,
        "photo_file_id": photo_id,
        "source_chat_id": CLIENT_ID,
        "source_message_id": 1,
    }
    assert "К какому обращению" in bot.texts_to(CLIENT_ID)[0]
    sent = bot.sent[-1]
    assert isinstance(sent, SentMessage)
    markup = sent.reply_markup
    assert {button.callback_data for row in markup.inline_keyboard for button in row} >= {
        "clientticket:17",
        "clientticket:18",
        "clientpending:cancel",
    }
    assert "DeleteMessage" not in bot.calls


async def test_message_without_an_open_ticket_points_to_start(
    clock: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot = FakeBot()
    monkeypatch.setattr(tickets_repo, "list_open_for_user", AsyncMock(return_value=[]))
    service = SimpleNamespace(
        is_manager=lambda _id: False,
        is_admin=lambda _id: False,
        is_staff=AsyncMock(return_value=False),
        ticket_for_reply=AsyncMock(return_value=None),
    )

    await user_flow.handle_client_message(
        _bound(private_message("привет", user_id=CLIENT_ID), bot),
        service,
        state=_fsm(CLIENT_ID, CLIENT_ID),
        session=SimpleNamespace(commit=AsyncMock()),
        bot=cast(Bot, bot),
        clock=clock,
    )

    assert "/start" in bot.texts_to(CLIENT_ID)[0]


class _StubQuery:
    def __init__(self, data: str, user_id: int = CLIENT_ID) -> None:
        from aiogram.types import User

        self.data = data
        self.from_user = User(id=user_id, is_bot=False, first_name="Аня")
        self.message = None
        self.answer = AsyncMock()


@pytest.mark.parametrize(
    ("owner_id", "ticket_state", "expect_relay"),
    [(CLIENT_ID, "new", True), (777, "new", False), (CLIENT_ID, "closed", False)],
)
async def test_pending_callback_validates_ticket_and_relays_once(
    owner_id: int,
    ticket_state: str,
    expect_relay: bool,
    clock: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = FakeBot()
    state = _fsm(CLIENT_ID, CLIENT_ID)
    await state.update_data(
        pending_client_message={
            "text": "подпись",
            "photo_file_id": "photo-pending",
            "source_chat_id": CLIENT_ID,
            "source_message_id": 7,
        }
    )
    session = SimpleNamespace(commit=AsyncMock())
    service = SimpleNamespace(
        is_manager=lambda _id: False,
        is_admin=lambda _id: False,
        is_staff=AsyncMock(return_value=False),
        relay_from_client=AsyncMock(return_value=SimpleNamespace(ok=True)),
    )
    monkeypatch.setattr(
        tickets_repo,
        "get_ticket",
        AsyncMock(
            return_value=SimpleNamespace(id=17, telegram_user_id=owner_id, state=ticket_state)
        ),
    )
    render_ticket = AsyncMock(return_value=True)
    monkeypatch.setattr(user_flow, "_render_ticket", render_ticket)
    query = _StubQuery("clientticket:17")

    await user_flow.select_ticket_for_pending_message(
        query, state, session, cast(Bot, bot), service, clock
    )

    assert service.relay_from_client.await_count == int(expect_relay)
    assert ("DeleteMessage" in bot.calls) is expect_relay
    if expect_relay:
        relay_kwargs = service.relay_from_client.await_args.kwargs
        assert relay_kwargs["ticket_id"] == 17
        assert relay_kwargs["text"] == "подпись"
        assert relay_kwargs["photo_file_id"] == "photo-pending"
        render_ticket.assert_awaited_once()
        assert await state.get_data() == {"ticket_id": 17}
    else:
        query.answer.assert_awaited_once_with(
            "Выбор устарел. Отправьте сообщение ещё раз.", show_alert=True
        )
        assert (await state.get_data())["pending_client_message"]["text"] == "подпись"


async def test_repeated_callback_cannot_relay_consumed_message(
    clock: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot = FakeBot()
    state = _fsm(CLIENT_ID, CLIENT_ID)
    await state.update_data(
        pending_client_message={"text": "сохранённый текст", "photo_file_id": None}
    )
    session = SimpleNamespace(commit=AsyncMock())
    service = SimpleNamespace(
        is_manager=lambda _id: False,
        is_admin=lambda _id: False,
        is_staff=AsyncMock(return_value=False),
        relay_from_client=AsyncMock(return_value=SimpleNamespace(ok=True)),
    )
    monkeypatch.setattr(
        tickets_repo,
        "get_ticket",
        AsyncMock(return_value=SimpleNamespace(id=17, telegram_user_id=CLIENT_ID, state="new")),
    )
    monkeypatch.setattr(user_flow, "_render_ticket", AsyncMock(return_value=True))
    query = _StubQuery("clientticket:17")

    await user_flow.select_ticket_for_pending_message(
        query, state, session, cast(Bot, bot), service, clock
    )
    await user_flow.select_ticket_for_pending_message(
        query, state, session, cast(Bot, bot), service, clock
    )

    assert service.relay_from_client.await_count == 1


async def test_pending_message_is_not_overwritten_by_another_unprompted_message(
    clock: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot = FakeBot()
    monkeypatch.setattr(
        tickets_repo,
        "list_open_for_user",
        AsyncMock(
            return_value=[
                SimpleNamespace(id=17, title="Заказ", description="Первое"),
                SimpleNamespace(id=18, title="Доставка", description="Второе"),
            ]
        ),
    )
    monkeypatch.setattr(ui_repo, "list_messages", AsyncMock(return_value=[]))
    monkeypatch.setattr(ui_repo, "forget_all", AsyncMock())
    monkeypatch.setattr(ui_repo, "remember", AsyncMock())
    state = _fsm(CLIENT_ID, CLIENT_ID)
    await state.set_state(user_flow.ClientFlow.choosing_ticket)
    await state.update_data(
        pending_client_message={"text": "уже сохранено", "photo_file_id": "photo-1"}
    )
    service = SimpleNamespace(
        is_manager=lambda _id: False,
        is_admin=lambda _id: False,
        is_staff=AsyncMock(return_value=False),
        ticket_for_reply=AsyncMock(return_value=None),
    )

    await user_flow.handle_client_message(
        _bound(private_message("новое", user_id=CLIENT_ID), bot),
        service,
        state=state,
        session=SimpleNamespace(commit=AsyncMock()),
        bot=cast(Bot, bot),
        clock=clock,
    )

    assert (await state.get_data())["pending_client_message"] == {
        "text": "уже сохранено",
        "photo_file_id": "photo-1",
    }
    assert "после выбора чата отправьте его повторно" in bot.texts_to(CLIENT_ID)[0]
    assert "DeleteMessage" not in bot.calls
    service.ticket_for_reply.assert_not_awaited()


async def test_staff_database_role_cannot_use_direct_client_callback(
    clock: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot = FakeBot()
    state = _fsm(CLIENT_ID, CLIENT_ID)
    await state.update_data(pending_client_message={"text": "текст", "photo_file_id": None})
    service = SimpleNamespace(
        is_manager=lambda _id: False,
        is_admin=lambda _id: False,
        is_staff=AsyncMock(return_value=True),
        relay_from_client=AsyncMock(return_value=SimpleNamespace(ok=True)),
    )
    monkeypatch.setattr(
        tickets_repo,
        "get_ticket",
        AsyncMock(return_value=SimpleNamespace(id=17, telegram_user_id=CLIENT_ID, state="new")),
    )
    query = _StubQuery("clientticket:17")

    await user_flow.select_ticket_for_pending_message(
        query, state, SimpleNamespace(commit=AsyncMock()), cast(Bot, bot), service, clock
    )

    service.relay_from_client.assert_not_awaited()
    query.answer.assert_awaited_once_with("Сотрудникам недоступен клиентский чат", show_alert=True)


async def test_staff_database_role_cannot_use_direct_client_message_route(clock: Any) -> None:
    bot = FakeBot()
    service = SimpleNamespace(
        is_manager=lambda _id: False,
        is_admin=lambda _id: False,
        is_staff=AsyncMock(return_value=True),
        ticket_for_reply=AsyncMock(return_value=SimpleNamespace(id=17, state="new")),
        relay_from_client=AsyncMock(return_value=SimpleNamespace(ok=True)),
    )

    await user_flow.handle_client_message(
        _bound(private_message("текст", user_id=CLIENT_ID), bot),
        service,
        state=_fsm(CLIENT_ID, CLIENT_ID),
        session=SimpleNamespace(commit=AsyncMock()),
        bot=cast(Bot, bot),
        clock=clock,
    )

    service.ticket_for_reply.assert_not_awaited()
    service.relay_from_client.assert_not_awaited()


async def test_relay_failure_reports_saved_but_not_delivered(
    clock: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot = FakeBot()
    state = _fsm(CLIENT_ID, CLIENT_ID)
    await state.update_data(
        pending_client_message={"text": "сохранённый текст", "photo_file_id": None}
    )
    session = SimpleNamespace(commit=AsyncMock())
    service = SimpleNamespace(
        is_manager=lambda _id: False,
        is_admin=lambda _id: False,
        is_staff=AsyncMock(return_value=False),
        relay_from_client=AsyncMock(return_value=SimpleNamespace(ok=False)),
    )
    monkeypatch.setattr(
        tickets_repo,
        "get_ticket",
        AsyncMock(return_value=SimpleNamespace(id=17, telegram_user_id=CLIENT_ID, state="new")),
    )
    fake_ui = SimpleNamespace(render=AsyncMock(), remove_incoming=AsyncMock())
    monkeypatch.setattr(user_flow, "_ui", lambda _clock: fake_ui)
    query = _StubQuery("clientticket:17")

    await user_flow.select_ticket_for_pending_message(
        query, state, session, cast(Bot, bot), service, clock
    )

    query.answer.assert_awaited_once_with("Сообщение сохранено, но не доставлено", show_alert=True)
    assert "не удалось его доставить" in fake_ui.render.await_args.kwargs["parts"][0]


async def test_single_ticket_relay_failure_reports_saved_but_not_delivered(
    clock: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot = FakeBot()
    fake_ui = SimpleNamespace(render=AsyncMock(), remove_incoming=AsyncMock())
    monkeypatch.setattr(user_flow, "_ui", lambda _clock: fake_ui)
    monkeypatch.setattr(
        tickets_repo,
        "get_ticket",
        AsyncMock(return_value=SimpleNamespace(id=17, telegram_user_id=CLIENT_ID, state="new")),
    )
    service = SimpleNamespace(
        is_manager=lambda _id: False,
        is_admin=lambda _id: False,
        is_staff=AsyncMock(return_value=False),
        ticket_for_reply=AsyncMock(return_value=SimpleNamespace(id=17, state="new")),
        relay_from_client=AsyncMock(return_value=SimpleNamespace(ok=False)),
    )

    await user_flow.handle_client_message(
        _bound(private_message("текст", user_id=CLIENT_ID), bot),
        service,
        state=_fsm(CLIENT_ID, CLIENT_ID),
        session=SimpleNamespace(commit=AsyncMock()),
        bot=cast(Bot, bot),
        clock=clock,
    )

    assert "не удалось его доставить" in fake_ui.render.await_args.kwargs["parts"][0]


async def test_start_category_payload_accepts_active_database_category(
    app_config: AppConfig, clock: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(staff_repo, "activate_invitation", AsyncMock(return_value=None))
    monkeypatch.setattr(
        content_repo,
        "category",
        AsyncMock(return_value=SimpleNamespace(key="db_only", is_active=True)),
    )
    state = _fsm(CLIENT_ID, CLIENT_ID)
    bot = FakeBot()

    await user_flow.handle_start(
        _bound(private_message("/start db_only", user_id=CLIENT_ID), bot),
        state,
        app_config,
        SimpleNamespace(
            is_manager=lambda _id: False,
            is_admin=lambda _id: False,
            is_staff=AsyncMock(return_value=False),
        ),
        session=object(),
    )

    assert await state.get_state() == user_flow.ClientFlow.waiting_description.state
    assert (await state.get_data())["category"] == "db_only"


async def test_database_staff_cannot_start_client_flow_directly(
    app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(staff_repo, "activate_invitation", AsyncMock(return_value=None))
    state = _fsm(CLIENT_ID, CLIENT_ID)
    bot = FakeBot()
    service = SimpleNamespace(
        is_manager=lambda _id: False,
        is_admin=lambda _id: False,
        is_staff=AsyncMock(return_value=True),
    )

    await user_flow.handle_start(
        _bound(private_message("/start defect", user_id=CLIENT_ID), bot),
        state,
        app_config,
        service,
        session=object(),
    )

    assert await state.get_state() is None
    assert "сотрудника" in bot.texts_to(CLIENT_ID)[0]


async def test_start_preserves_pending_message_without_activating_invitation(
    app_config: AppConfig, clock: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    activate = AsyncMock(return_value=None)
    monkeypatch.setattr(staff_repo, "activate_invitation", activate)
    monkeypatch.setattr(
        tickets_repo,
        "list_open_for_user",
        AsyncMock(return_value=[SimpleNamespace(id=17, title="Заказ", description="Первое")]),
    )
    monkeypatch.setattr(ui_repo, "list_messages", AsyncMock(return_value=[]))
    monkeypatch.setattr(ui_repo, "forget_all", AsyncMock())
    monkeypatch.setattr(ui_repo, "remember", AsyncMock())
    state = _fsm(CLIENT_ID, CLIENT_ID)
    await state.set_state(user_flow.ClientFlow.choosing_ticket)
    pending = {
        "text": "сообщение осталось",
        "photo_file_id": "photo-1",
        "source_chat_id": CLIENT_ID,
        "source_message_id": 3,
    }
    await state.update_data(pending_client_message=pending)
    bot = FakeBot()
    service = SimpleNamespace(
        is_manager=lambda _id: False,
        is_admin=lambda _id: False,
        is_staff=AsyncMock(return_value=False),
    )

    await user_flow.handle_start(
        _bound(private_message("/start", user_id=CLIENT_ID, message_id=4), bot),
        state,
        app_config,
        service,
        session=SimpleNamespace(commit=AsyncMock()),
        bot=cast(Bot, bot),
        clock=clock,
    )

    activate.assert_not_awaited()
    assert (await state.get_data())["pending_client_message"] == pending
    assert await state.get_state() == user_flow.ClientFlow.choosing_ticket.state
    sent = bot.sent[-1]
    assert isinstance(sent, SentMessage)
    assert "Отменить отправку" in bot.buttons(sent)
    assert "выберите обращение" in bot.texts_to(CLIENT_ID)[0]


async def test_back_to_tickets_keeps_pending_message_and_reoffers_cancel(
    clock: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        tickets_repo,
        "list_open_for_user",
        AsyncMock(return_value=[SimpleNamespace(id=17, title="Заказ", description="Первое")]),
    )
    monkeypatch.setattr(
        tickets_repo,
        "list_for_user",
        AsyncMock(return_value=[SimpleNamespace(id=17, description="Первое")]),
    )
    monkeypatch.setattr(ui_repo, "list_messages", AsyncMock(return_value=[]))
    monkeypatch.setattr(ui_repo, "forget_all", AsyncMock())
    monkeypatch.setattr(ui_repo, "remember", AsyncMock())
    state = _fsm(CLIENT_ID, CLIENT_ID)
    await state.set_state(user_flow.ClientFlow.choosing_ticket)
    await state.update_data(pending_client_message={"text": "не терять", "photo_file_id": None})
    bot = FakeBot()
    query = _StubQuery("sup:tickets")

    await user_flow.client_ticket_list(
        query, state, SimpleNamespace(commit=AsyncMock()), cast(Bot, bot), clock
    )

    assert (await state.get_data())["pending_client_message"]["text"] == "не терять"
    assert await state.get_state() == user_flow.ClientFlow.choosing_ticket.state
    sent = bot.sent[-1]
    assert isinstance(sent, SentMessage) and sent.reply_markup is not None
    assert "Мои обращения" in sent.text
    assert "clientpending:cancel" in {
        button.callback_data for row in sent.reply_markup.inline_keyboard for button in row
    }


async def test_explicit_cancel_clears_pending_and_returns_to_ticket_list(
    clock: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tickets_repo, "list_for_user", AsyncMock(return_value=[]))
    monkeypatch.setattr(ui_repo, "list_messages", AsyncMock(return_value=[]))
    monkeypatch.setattr(ui_repo, "forget_all", AsyncMock())
    monkeypatch.setattr(ui_repo, "remember", AsyncMock())
    state = _fsm(CLIENT_ID, CLIENT_ID)
    await state.set_state(user_flow.ClientFlow.choosing_ticket)
    await state.update_data(
        pending_client_message={
            "text": "не отправлять",
            "photo_file_id": "photo-1",
            "source_chat_id": CLIENT_ID,
            "source_message_id": 55,
        }
    )
    bot = FakeBot()
    service = SimpleNamespace(is_staff=AsyncMock(return_value=False))
    query = _StubQuery("clientpending:cancel")

    await user_flow.cancel_pending_client_message(
        query, state, SimpleNamespace(commit=AsyncMock()), cast(Bot, bot), service, clock
    )

    assert await state.get_data() == {}
    assert await state.get_state() is None
    query.answer.assert_awaited_once_with("Отправка отменена")
    assert bot.texts_to(CLIENT_ID) == ["У вас пока нет обращений."]
    assert "DeleteMessage" not in bot.calls
