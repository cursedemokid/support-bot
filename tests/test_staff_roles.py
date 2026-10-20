from typing import Any, cast

from aiogram import Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import User

from app.bot.handlers import admin_content
from app.repositories.staff import normalize_username
from tests.factories import MANAGER_ID, FakeBot


def test_normalize_username_removes_at_and_lowercases() -> None:
    assert normalize_username(" @redacted_user ") == "support_one"


class _AdminQuery:
    from_user = User(id=MANAGER_ID, is_bot=False, first_name="Администратор")
    data: str | None = None

    async def answer(self, *_: Any, **__: Any) -> None:
        return None


async def test_admin_navigation_replaces_previous_screen_and_has_back(
    session_factory, clock
) -> None:
    bot = FakeBot()
    query = _AdminQuery()
    state = FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=1, chat_id=MANAGER_ID, user_id=MANAGER_ID),
    )

    async with session_factory() as session:
        await admin_content.menu(query, session, cast(Bot, bot), clock, state)
        await admin_content.staff_menu(query, session, cast(Bot, bot), clock, state)

    assert "DeleteMessage" in bot.calls
    last = bot.sent[-1]
    buttons = [button for row in last.reply_markup.inline_keyboard for button in row]
    assert (buttons[-1].text, buttons[-1].callback_data) == ("⬅️ Назад", "mgr:home")
