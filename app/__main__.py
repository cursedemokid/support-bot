"""Точка входа: конфиг → база → бот.

Отказы различаются кодами выхода, чтобы в systemd сразу было видно, что
чинить: конфиг, база, права на схему или токен.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from aiogram.exceptions import TelegramUnauthorizedError
from aiogram.utils.token import TokenValidationError
from loguru import logger

from app.bot.factory import build_bot, build_dispatcher, resolve_allowed_updates
from app.clock import SystemClock
from app.config import AppConfig, ConfigError, load_secrets, load_settings
from app.db.engine import build_engine, check_connection
from app.db.session import build_session_factory
from app.logging import setup_logging

EXIT_BAD_CONFIG = 2
EXIT_NO_DATABASE = 3
EXIT_BAD_TOKEN = 4


async def run(config: AppConfig) -> None:
    clock = SystemClock(config.settings.tzinfo)
    engine = build_engine(config.secrets.database_url)

    try:
        await check_connection(engine)
    except Exception as exc:
        logger.error("База недоступна по DATABASE_URL: {}", exc)
        await engine.dispose()
        raise SystemExit(EXIT_NO_DATABASE) from exc

    session_factory = build_session_factory(engine)
    logger.info("База отвечает, пояс — {}", clock.tz.key)

    # Токен проверяет сам aiogram ещё до сети — на форме строки. Дать этому
    # вылететь стектрейсом значит спрятать причину («в .env плейсхолдер»)
    # за шестью кадрами чужого кода, а причина тут ровно та же, что у
    # EXIT_BAD_TOKEN.
    try:
        bot = build_bot(config)
    except TokenValidationError as exc:
        logger.error("BOT_TOKEN не похож на токен бота: {}", exc)
        await engine.dispose()
        raise SystemExit(EXIT_BAD_TOKEN) from exc

    dispatcher = build_dispatcher(config, clock, session_factory)

    try:
        me = await bot.get_me()
    except TelegramUnauthorizedError as exc:
        logger.error("Telegram не принял BOT_TOKEN: проверь значение в .env")
        await bot.session.close()
        await engine.dispose()
        raise SystemExit(EXIT_BAD_TOKEN) from exc
    logger.info("Запускаемся как @{} (id {})", me.username, me.id)

    managers = config.settings.support.manager_user_ids
    if not managers:
        logger.warning("support.manager_user_ids пуст — обращения некому отправлять")

    stop_reason = "поллинг завершён штатно"
    try:
        await dispatcher.start_polling(bot, allowed_updates=resolve_allowed_updates(dispatcher))
    except (KeyboardInterrupt, SystemExit):
        stop_reason = "получен сигнал остановки"
        raise
    except Exception as exc:
        stop_reason = f"поллинг упал: {type(exc).__name__}: {exc}"
        raise
    finally:
        logger.info("Остановлены: {}", stop_reason)
        await bot.session.close()
        await engine.dispose()


def main() -> None:
    settings_path = Path("config/settings.yaml")
    try:
        settings = load_settings(settings_path)
        secrets = load_secrets()
    except ConfigError as exc:
        sys.stderr.write(f"\n{exc}\n\n")
        raise SystemExit(EXIT_BAD_CONFIG) from None

    setup_logging(
        level=secrets.log_level,
        sentry_dsn=secrets.sentry_dsn,
        bot_token=secrets.bot_token,
    )
    config = AppConfig(settings, secrets, path=settings_path)
    asyncio.run(run(config))


if __name__ == "__main__":
    main()
