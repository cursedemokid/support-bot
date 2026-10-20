"""Подхват правок конфигурации без перезапуска.

Проверяется содержимое небольшого YAML-файла, поэтому быстрые перезаписи с тем
же `mtime` тоже обнаруживаются. Битый файл перезагрузку не ломает: остаётся
прежняя конфигурация, в лог уходит ошибка один раз.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from app.config import AppConfig


class ConfigReloadMiddleware(BaseMiddleware):
    def __init__(self, config: AppConfig) -> None:
        self._config = config

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        self._config.reload_if_changed()
        return await handler(event, data)
