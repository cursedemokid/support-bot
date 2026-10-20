"""Сборка Bot и Dispatcher."""

from __future__ import annotations

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import SimpleEventIsolation
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot.handlers import build_root_router
from app.bot.middlewares.config_reload import ConfigReloadMiddleware
from app.bot.middlewares.database import DbSessionMiddleware
from app.bot.middlewares.logging import LoggingContextMiddleware
from app.clock import Clock
from app.config import AppConfig
from app.errors import register_error_handler
from app.logging import log_event
from app.services.tickets import TicketService


def build_bot(config: AppConfig) -> Bot:
    return Bot(
        token=config.secrets.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


def build_dispatcher(
    config: AppConfig,
    clock: Clock,
    session_factory: async_sessionmaker[AsyncSession],
) -> Dispatcher:
    # config/clock/service кладутся в workflow_data: хендлеры и сервисы
    # получают их аргументами, а не тянут через глобальные переменные.
    service = TicketService(config, clock)
    dispatcher = Dispatcher(
        config=config,
        clock=clock,
        service=service,
        events_isolation=SimpleEventIsolation(),
    )

    # Порядок: контекст лога → перезагрузка конфига → сессия. Перезагрузка
    # раньше сессии намеренно — у неё нет своей транзакции, чужую она не трогает.
    dispatcher.update.outer_middleware(LoggingContextMiddleware())
    dispatcher.update.outer_middleware(ConfigReloadMiddleware(config))
    dispatcher.update.outer_middleware(DbSessionMiddleware(session_factory))

    dispatcher.include_router(build_root_router())

    # Исключение в обработчике логируется и уходит в Sentry, бот продолжает
    # принимать сообщения.
    register_error_handler(dispatcher)
    return dispatcher


def resolve_allowed_updates(dispatcher: Dispatcher) -> list[str]:
    """Типы апдейтов, под которые есть обработчики.

    Список пишется в лог на INFO: отсутствие подписки на нужный тип выглядит
    не сбоем, а тишиной — одна строка при старте отвечает на это сразу.
    """
    updates = sorted(dispatcher.resolve_used_update_types())
    log_event("INFO", "Подписка на апдейты Telegram: " + ", ".join(updates), action="bot.updates")
    return updates
