"""Фильтры, зависящие от конфига: чат менеджеров и права менеджера.

Фильтры, а не middleware: фильтр, не совпав, отдаёт апдейт соседним роутерам,
а middleware его гасит. Клиент, случайно написавший в чат менеджеров, должен
дойти до общего обработчика, а не исчезнуть молча.

`config` — не поле фильтра, а аргумент вызова: aiogram передаёт фильтрам
workflow data, куда `Dispatcher(config=...)` кладёт конфигурацию. Экземпляр
создаётся на импорте и потому не может знать о конфиге ничего.
"""

from __future__ import annotations

from aiogram.filters import BaseFilter
from aiogram.types import TelegramObject
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import AppConfig
from app.repositories import staff as staff_repo


class IsManager(BaseFilter):
    """Апдейт от человека из support.manager_user_ids."""

    async def __call__(
        self, event: TelegramObject, config: AppConfig, session: AsyncSession
    ) -> bool:
        user = getattr(event, "from_user", None)
        if user is None:
            return False
        allowed = (
            *config.settings.support.manager_user_ids,
            *config.settings.support.admin_user_ids,
        )
        if user.id in allowed:
            return True
        return await staff_repo.active_by_telegram_id(session, user.id) is not None


class IsAdmin(BaseFilter):
    """Апдейт от человека из support.admin_user_ids."""

    async def __call__(
        self, event: TelegramObject, config: AppConfig, session: AsyncSession
    ) -> bool:
        user = getattr(event, "from_user", None)
        if user is None:
            return False
        if user.id in config.settings.support.admin_user_ids:
            return True
        member = await staff_repo.active_by_telegram_id(session, user.id)
        return member is not None and member.role == staff_repo.ADMIN


class IsClient(BaseFilter):
    """Клиентский кабинет не доступен сотрудникам даже по поддельному callback."""

    async def __call__(
        self, event: TelegramObject, config: AppConfig, session: AsyncSession
    ) -> bool:
        user = getattr(event, "from_user", None)
        if user is None:
            return False
        support = config.settings.support
        if user.id in (*support.manager_user_ids, *support.admin_user_ids):
            return False
        return await staff_repo.active_by_telegram_id(session, user.id) is None
