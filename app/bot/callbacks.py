"""Грамматика `callback_data`: и сборка, и разбор в одном файле.

Половина формата в клавиатуре и половина в хендлере однажды разъезжаются:
добавили кнопку, забыли разбор — нажатие тихо ничего не делает. Здесь обе
половины меняются вместе, а `parse` возвращает None на всё, что под формат
не подходит (мусор, подделка, опечатка).

Формат:

    cat:<key>               клиент выбрал категорию <key>
    tk:take:<id>            менеджер берёт тикет <id>
    tk:close:<id>           менеджер закрывает тикет <id>

`<key>` — ключ категории из config/settings.yaml: латиница, цифры,
подчёркивание, до 32 символов. `<id>` — числовой id тикета.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from aiogram.filters import BaseFilter
from aiogram.types import TelegramObject

PREFIX_CATEGORY = "cat"
PREFIX_TICKET = "tk"

CATEGORY_KEY = re.compile(r"^[a-z0-9_]{1,32}$")


@dataclass(frozen=True, slots=True)
class PickCategory:
    """Клиент нажал категорию."""

    key: str


@dataclass(frozen=True, slots=True)
class TakeTicket:
    """Менеджер берёт тикет в работу."""

    ticket_id: int


@dataclass(frozen=True, slots=True)
class CloseTicket:
    """Менеджер закрывает тикет."""

    ticket_id: int


Parsed = PickCategory | TakeTicket | CloseTicket


def category_cb(key: str) -> str:
    return f"{PREFIX_CATEGORY}:{key}"


def take_cb(ticket_id: int) -> str:
    return f"{PREFIX_TICKET}:take:{ticket_id}"


def close_cb(ticket_id: int) -> str:
    return f"{PREFIX_TICKET}:close:{ticket_id}"


def _ticket_id(raw: str) -> int | None:
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def parse(data: str) -> Parsed | None:
    """None — что угодно, не подходящее под формат.

    Существование тикета и категории здесь не проверяется: это знает
    хендлер и конфиг, а не грамматика. Проверяется только форма.
    """
    parts = data.split(":")

    if len(parts) == 2 and parts[0] == PREFIX_CATEGORY:
        return PickCategory(parts[1]) if CATEGORY_KEY.match(parts[1]) else None

    if len(parts) == 3 and parts[0] == PREFIX_TICKET:
        ticket_id = _ticket_id(parts[2])
        if ticket_id is None:
            return None
        if parts[1] == "take":
            return TakeTicket(ticket_id)
        if parts[1] == "close":
            return CloseTicket(ticket_id)

    return None


class Callback(BaseFilter):
    """Пропустить нажатие нужного вида и подсунуть разобранные данные.

    Без этого фильтра aiogram не знает про наш формат: он не разбирает
    `callback_data` сам (в отличие от `CallbackData`-фабрик). Хендлер получает
    готовый `callback_data` аргументом — и разбор остаётся в одном месте.

    Подделка и мусор отсеиваются здесь же: нажатие с чужим `data` до хендлера
    не доходит, а не падает в нём.
    """

    def __init__(self, *types: type[Parsed]) -> None:
        self._types = types

    async def __call__(self, event: TelegramObject) -> dict[str, Parsed] | bool:
        data = getattr(event, "data", None)
        if not isinstance(data, str):
            return False
        parsed = parse(data)
        if parsed is None or not isinstance(parsed, self._types):
            return False
        return {"callback_data": parsed}
