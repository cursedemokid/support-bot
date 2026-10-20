"""Сборка объектов Telegram из словарей — в том виде, в каком их шлёт API.

`FakeBot` закрывает оба пути: прямые вызовы (`send_message` — так зовёт
сервис) и эмиссию через `bot(method)` (так работают `message.answer`,
`query.answer`, `edit_reply_markup`). Второй путь нужен тестам хендлеров:
они вызывают методы объектов aiogram, а те уходят в Bot.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, Message, User

CHAT_ID = -900000001
USER_ID = 42
# Менеджер из tests/fixtures/settings.yaml: сообщения в чате менеджеров должны
# идти от того, у кого есть права, — иначе тест проверяет не то, что настроено.
MANAGER_ID = 555
TS = 3738021089  # 2025-09-16 05:20 UTC


def make_message(**overrides: Any) -> Message:
    payload: dict[str, Any] = {
        "message_id": 1,
        "date": TS,
        "chat": {"id": CHAT_ID, "type": "supergroup", "title": "Поддержка"},
        "from": {"id": USER_ID, "is_bot": False, "first_name": "Аня", "username": "anya"},
    }
    payload.update(overrides)
    return Message.model_validate(payload)


def private_message(
    text: str | None,
    *,
    user_id: int = USER_ID,
    username: str | None = "anya",
    message_id: int = 1,
    **overrides: Any,
) -> Message:
    """Личное сообщение клиента. `username=None` — у клиента нет ника."""
    author: dict[str, Any] = {"id": user_id, "is_bot": False, "first_name": "Аня"}
    if username is not None:
        author["username"] = username
    return make_message(
        message_id=message_id,
        text=text,
        chat={"id": user_id, "type": "private", "first_name": "Аня"},
        **{"from": author},
        **overrides,
    )


def manager_message(
    text: str,
    *,
    reply_to: Message | None = None,
    user_id: int = MANAGER_ID,
    **overrides: Any,
) -> Message:
    """Сообщение в чате менеджеров, при необходимости — ответом на карточку."""
    overrides.setdefault("chat", {"id": CHAT_ID, "type": "supergroup", "title": "Поддержка"})
    overrides.setdefault(
        "from", {"id": user_id, "is_bot": False, "first_name": "Менеджер", "username": "mgr"}
    )
    return make_message(text=text, reply_to_message=reply_to, **overrides)


@dataclass(frozen=True, slots=True)
class SentMessage:
    """Что нужно сервису и тестам от ответа Telegram."""

    chat: Chat
    message_id: int
    text: str
    reply_markup: Any = None


@dataclass(frozen=True, slots=True)
class SentPhoto:
    chat: Chat
    message_id: int
    photo: str
    caption: str | None
    reply_markup: Any = None


@dataclass(frozen=True, slots=True)
class SentDocument:
    chat: Chat
    message_id: int
    filename: str | None
    data: bytes | str
    caption: str | None


class FakeBot:
    """Замена `Bot`: в Telegram не ходит, отправленное записывает.

    Не `Mock`: тестам нужны настоящие `chat.id`/`message_id` отправленных
    сообщений — по ним проверяется, что маршрут ответа записан верно, и что
    уведомление ушло именно клиенту, а не в чат менеджеров.
    """

    # aiogram читает `bot.id` при разборе апдейта (StorageKey для FSM).
    id = 1

    def __init__(self, *, fail_send: bool = False) -> None:
        self.sent: list[SentMessage | SentPhoto | SentDocument] = []
        self.calls: list[str] = []
        self._next_id = 1000
        self._fail_send = fail_send

    async def get_me(self) -> Any:
        from aiogram.types import User as TgUser

        return TgUser(id=self.id, is_bot=True, first_name="bot", username="support_bot")

    async def send_message(
        self,
        chat_id: int,
        text: str,
        reply_markup: object | None = None,
        **kwargs: Any,
    ) -> SentMessage:
        return self._deliver(chat_id, text, reply_markup)

    async def send_photo(
        self,
        chat_id: int,
        photo: str,
        caption: str | None = None,
        reply_markup: object | None = None,
        **kwargs: Any,
    ) -> SentPhoto:
        if self._fail_send:
            from aiogram.exceptions import TelegramBadRequest

            raise TelegramBadRequest(method=None, message="chat not found")  # type: ignore[arg-type]
        self._next_id += 1
        sent = SentPhoto(
            chat=Chat(id=chat_id, type="private" if chat_id > 0 else "supergroup"),
            message_id=self._next_id,
            photo=photo,
            caption=caption,
            reply_markup=reply_markup,
        )
        self.sent.append(sent)
        return sent

    async def send_document(
        self,
        chat_id: int,
        document: Any,
        caption: str | None = None,
        **kwargs: Any,
    ) -> SentDocument:
        self._next_id += 1
        data = getattr(document, "data", document)
        sent = SentDocument(
            chat=Chat(id=chat_id, type="private" if chat_id > 0 else "supergroup"),
            message_id=self._next_id,
            filename=getattr(document, "filename", None),
            data=data,
            caption=caption,
        )
        self.sent.append(sent)
        return sent

    async def download(self, file: Any, destination: Any, **kwargs: Any) -> Any:
        import base64

        file_id = getattr(file, "file_id", file)
        if not isinstance(file_id, str) or not file_id.startswith("payload:"):
            raise ValueError("unknown test file")
        destination.write(base64.b64decode(file_id.removeprefix("payload:")))
        destination.seek(0)
        return destination

    async def delete_message(self, chat_id: int, message_id: int) -> bool:
        self.calls.append("DeleteMessage")
        return True

    async def __call__(self, method: Any, request_timeout: float | None = None) -> Any:
        """Эмиссия методов aiogram: `message.answer`, `query.answer` и прочие."""
        name = type(method).__name__
        self.calls.append(name)
        if name == "SendMessage":
            return self._deliver(method.chat_id, method.text or "", method.reply_markup)
        return True

    def _deliver(self, chat_id: int, text: str, reply_markup: Any = None) -> SentMessage:
        if self._fail_send:
            from aiogram.exceptions import TelegramBadRequest

            raise TelegramBadRequest(method=None, message="chat not found")  # type: ignore[arg-type]
        self._next_id += 1
        sent = SentMessage(
            chat=Chat(id=chat_id, type="private" if chat_id > 0 else "supergroup"),
            message_id=self._next_id,
            text=text,
            reply_markup=reply_markup,
        )
        self.sent.append(sent)
        return sent

    @property
    def texts(self) -> list[str]:
        return [m.text for m in self.sent if isinstance(m, SentMessage)]

    def to(self, chat_id: int) -> list[SentMessage | SentPhoto | SentDocument]:
        """Сообщения, ушедшие в конкретный чат: клиенту или менеджерам."""
        return [m for m in self.sent if m.chat.id == chat_id]

    def texts_to(self, chat_id: int) -> list[str]:
        return [m.text for m in self.to(chat_id) if isinstance(m, SentMessage)]

    def photos_to(self, chat_id: int) -> list[SentPhoto]:
        return [m for m in self.to(chat_id) if isinstance(m, SentPhoto)]

    def documents_to(self, chat_id: int) -> list[SentDocument]:
        return [m for m in self.to(chat_id) if isinstance(m, SentDocument)]

    def buttons(self, message: SentMessage) -> list[str]:
        """Подписи кнопок отправленного сообщения — если клавиатура была."""
        markup = message.reply_markup
        if markup is None:
            return []
        return [b.text for row in markup.inline_keyboard for b in row]


def admin_callback(data: str, user_id: int = MANAGER_ID) -> CallbackQuery:
    return CallbackQuery(
        id="admin-callback",
        from_user=User(id=user_id, is_bot=False, first_name="Админ"),
        chat_instance="admin-chat",
        data=data,
    )


def admin_fsm(user_id: int = MANAGER_ID) -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id),
    )


def document_message(
    payload: bytes,
    *,
    filename: str,
    mime_type: str,
    user_id: int = MANAGER_ID,
) -> Message:
    import base64

    return private_message(
        None,
        user_id=user_id,
        document={
            "file_id": "payload:" + base64.b64encode(payload).decode("ascii"),
            "file_unique_id": "test-document",
            "file_name": filename,
            "mime_type": mime_type,
            "file_size": len(payload),
        },
    )


def broken_workbook_bytes() -> bytes:
    return b"not-an-xlsx"
