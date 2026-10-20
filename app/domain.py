"""Бизнес-типы: состояния и переходы без привязки к aiogram."""

from __future__ import annotations

from enum import StrEnum


class TicketState(StrEnum):
    """Состояние тикета. Строковое — для читаемого лога и простого SQL."""

    NEW = "new"
    ASSIGNED = "assigned"
    CLOSED = "closed"
