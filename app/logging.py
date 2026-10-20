"""Логирование: структурированный лог в stderr, вычистка токена, Sentry.

Текст сообщения клиента не попадает в лог никогда (белый список полей).
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Callable
from types import FrameType
from typing import Any, cast

from loguru import logger


class _InterceptHandler(logging.Handler):
    """Перехватчик stdlib-логирования → loguru."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level: str | int = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        frame: FrameType | None = logging.currentframe()
        depth = 2
        while frame and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1

        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


def _format_with_extra(record: Any) -> str:
    """Формат: время, уровень, место, сообщение, extra-поля если есть."""
    base = (
        "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
        "<level>{level: <8}</level> | "
        "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
        "<level>{message}</level>"
    )
    extra_items = [f"{k}={v}" for k, v in record["extra"].items() if k in ALLOWED_FIELDS]
    if extra_items:
        return base + " | " + " ".join(extra_items) + "\n"
    return base + "\n"


def _redact(event: Any, bot_token: str | None) -> Any:
    """Вычистить токен из URL и тела события Sentry."""
    if not bot_token:
        return event

    pattern = re.escape(bot_token)
    replacement = "***TOKEN***"

    def _scrub_value(val: Any) -> Any:
        if isinstance(val, str):
            return re.sub(pattern, replacement, val)
        if isinstance(val, dict):
            return {k: _scrub_value(v) for k, v in val.items()}
        if isinstance(val, list):
            return [_scrub_value(x) for x in val]
        return val

    return _scrub_value(event)


def setup_logging(
    level: str = "INFO",
    sentry_dsn: str | None = None,
    bot_token: str | None = None,
) -> None:
    logger.remove()
    logger.add(
        sys.stderr,
        level=level,
        format=cast(Callable[[Any], str], _format_with_extra),
        backtrace=False,
        diagnose=False,
    )

    logging.basicConfig(handlers=[_InterceptHandler()], level=0, force=True)
    for noisy in ("aiogram.event", "aiohttp.access", "sqlalchemy.engine"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    if sentry_dsn:
        import sentry_sdk
        from sentry_sdk.integrations.loguru import LoguruIntegration

        def scrub(event: Any, _hint: Any = None) -> Any:
            return _redact(event, bot_token)

        sentry_sdk.init(
            dsn=sentry_dsn,
            before_send=scrub,
            before_breadcrumb=scrub,
            integrations=[LoguruIntegration()],
            traces_sample_rate=0.0,
            send_default_pii=False,
            include_local_variables=False,
        )
        logger.info("Sentry подключён")
    else:
        logger.warning("SENTRY_DSN не задан — ошибки никуда не уедут")


# Белый список полей события: что можно показывать в логе.
# Текст сообщения клиента (`message_text`, `description`) не включаем никогда.
ALLOWED_FIELDS: frozenset[str] = frozenset(
    {
        "chat_id",
        "message_id",
        "telegram_user_id",
        "ticket_id",
        "category",
        "state",
        "manager_id",
        "action",
    }
)


def event_context(**fields: Any) -> dict[str, Any]:
    """Структурированные поля события — только из белого списка."""
    return {k: v for k, v in fields.items() if k in ALLOWED_FIELDS}


def log_event(level: str, message: str, **fields: Any) -> None:
    """Записать событие с полями. Единственный способ это делать.

    Поле вне белого списка не роняет обработку: опечатка в имени поля — не
    повод потерять сообщение клиента. Строка пишется без лишних полей, а про
    саму опечатку уходит WARNING — отказ громкий, а не молчаливый.

    `opt(depth=1)` — чтобы в логе стояло настоящее место вызова, а не
    `app.logging:log_event`.
    """
    unknown = set(fields) - ALLOWED_FIELDS
    if unknown:
        logger.opt(depth=1).warning("Поле вне белого списка лога: {}", sorted(unknown))
    logger.opt(depth=1).bind(**event_context(**fields)).log(level, message)
