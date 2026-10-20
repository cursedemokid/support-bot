"""Глобальный обработчик ошибок aiogram.

NFR-2: исключение внутри обработчика апдейта NOT SHALL останавливать бота —
оно логируется, и приём следующих сообщений продолжается как ни в чём
не бывало.
NFR-3: все ошибки уровня ERROR SHALL уходить в Sentry с контекстом chat_id
и telegram_user_id.

Как это работает: сам aiogram оборачивает весь конвейер обработки апдейта
(`Dispatcher.update.outer_middleware`) во встроенную `ErrorsMiddleware` — она
ловит любое исключение, вылетевшее из наших middleware или хендлеров
(`app/bot/...`, не наши файлы), и передаёт его сюда через observer
`dispatcher.errors` как `ErrorEvent`. Наш обработчик — последний рубеж перед
`Dispatcher.feed_update`: он ничего не поднимает дальше, поэтому polling
считает апдейт обработанным и забирает следующий как обычно — падение
одного обработчика не убивает процесс.

`event_chat`/`event_from_user` в сигнатуру подставляет сам aiogram: это те
же объекты, которые встроенный `UserContextMiddleware` кладёт в контекст
ДО вызова хендлера, — поэтому они доступны, даже когда упал хендлер, а не
наша `LoggingContextMiddleware` (её `logger.contextualize` уже размотан
к моменту, когда исключение долетает досюда).

Sentry получает контекст не отдельным вызовом `sentry_sdk`, а через уже
настроенный в `app/logging.py` (чужой файл, NFR-3 упомянут в его же
докстринге) `sentry_sdk.integrations.loguru.LoguruIntegration`: она сама
отправляет в Sentry любой лог уровня ERROR. Наша задача — залогировать
с правильным, безопасным контекстом через `event_context` (тот же белый
список полей, что и у остального кода) и не заводить второй, параллельный
путь отправки: вызов `sentry_sdk.capture_exception` вдобавок к ERROR-логу
задвоил бы событие в Sentry на одно и то же исключение.
"""

from __future__ import annotations

from aiogram import Dispatcher
from aiogram.types import Chat, ErrorEvent, User
from loguru import logger

from app.logging import event_context


async def handle_error(
    event: ErrorEvent,
    event_chat: Chat | None = None,
    event_from_user: User | None = None,
) -> bool:
    """Последний обработчик в цепочке: логирует и гасит исключение.

    aiogram считает апдейт обработанным, если error-хендлер вернул что-то,
    отличное от служебного sentinel `UNHANDLED` — конкретное значение не
    важно, `True` просто читается как «разобрались, не поднимай дальше».

    Текст сообщения сюда попасть не может: контекст собирается только из
    `chat_id`/`telegram_user_id` — оба просто числовые идентификаторы,
    прошедшие через белый список `event_context` (BRIEF-AI.md §3).
    """
    fields: dict[str, int] = {}
    if event_chat is not None:
        fields["chat_id"] = event_chat.id
    if event_from_user is not None:
        fields["telegram_user_id"] = event_from_user.id

    logger.bind(**event_context(**fields)).opt(exception=event.exception).error(
        "Необработанное исключение при обработке апдейта id={}: {}",
        event.update.update_id,
        event.exception,
    )
    return True


def register_error_handler(dispatcher: Dispatcher) -> None:
    """Точка подключения: вызвать один раз при сборке диспетчера.

    Сам вызов делает `app/__main__.py` (не наш файл) сразу после
    `build_dispatcher` — здесь только регистрация, без побочных эффектов.
    """
    dispatcher.errors.register(handle_error)
