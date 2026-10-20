# Требования

Функциональные (FR) и нефункциональные (NFR) требования бота поддержки.
Проверяемость — часть требования: у каждого пункта назван способ проверки.

## 1. Вход клиента

| № | Требование | Проверка |
|---|---|---|
| FR-1 | Клиент начинает обращение командой `/start` или по QR-ссылке | `tests/test_handlers.py::test_start_shows_categories` |
| FR-2 | В ответ показываются категории из `config/settings.yaml` кнопками | там же — сверка подписей кнопок с конфигом |
| FR-3 | Параметр QR, совпавший с ключом категории, выбирает её сразу | `test_start_with_category_payload_skips_choice` |
| FR-4 | Незнакомый параметр QR не ломает вход: клиент видит выбор категории | `test_start_with_unknown_payload_falls_back_to_choice` |
| FR-5 | Категория, убранная из конфига, не принимается | `test_reload_applies_change`, тесты `callbacks` |
| FR-6 | Описание короче или равно `max_message_chars` принимается целиком | `test_description_creates_ticket` |
| FR-7 | Описание длиннее предела отклоняется с указанием обоих чисел, выбранная категория сохраняется | `test_too_long_description_is_refused_and_state_kept` |
| FR-8 | Пустое сообщение не принимается | `handle_description` (ветка `not text`) |
| FR-9 | Менеджер, написавший боту в личку, не создаёт тикет сам себе | `test_start_by_manager_shows_hint` |
| FR-9.1 | Фото/файл вместо текста не оставляют клиента в тишине: бот отвечает, что передаёт только текст | `test_photo_while_describing_asks_for_text`, `test_photo_with_open_ticket_is_not_forwarded` |

## 2. Тикет

| № | Требование | Проверка |
|---|---|---|
| FR-10 | Тикет создаётся со состоянием `new`, автором и временем создания (`timestamptz`) | `test_create_ticket_records_client_and_manager_card` |
| FR-11 | Описание сохраняется первым сообщением истории | `test_create_ticket_writes_description_to_history` |
| FR-12 | Карточка тикета уходит в `manager_chat_id`: номер, категория, клиент, текст | `test_create_ticket_records_client_and_manager_card` |
| FR-13 | Недоступный чат менеджеров НЕ теряет обращение: тикет создаётся, клиент получает подтверждение без обещания «менеджер ответит» | `test_ticket_created_even_when_manager_chat_unreachable` |
| FR-14 | Клиент видит номер своего обращения | `test_description_creates_ticket` |

## 3. Работа менеджера

| № | Требование | Проверка |
|---|---|---|
| FR-15 | Взять тикет можно кнопкой под карточкой или командой `/take <N>` | `test_callback_buttons_take_ticket`, `test_take_by_command` |
| FR-16 | Взятие фиксирует `manager_id`, `assigned_at` и состояние `assigned` | `test_take_assigns_manager_and_notifies_client` |
| FR-17 | Взятый другим менеджером тикет взять нельзя | `test_take_twice_by_other_manager_is_refused` |
| FR-18 | Повторное взятие своего же тикета — понятный отказ, а не ошибка | `TicketService.take` (ветка «Ты уже ведёшь») |
| FR-19 | Несуществующий номер — отказ, а не исключение | `test_take_missing_ticket_is_refused` |
| FR-20 | Команда без номера показывает формат, а не молчит | `test_take_without_valid_id_shows_format` |
| FR-21 | Ответ менеджера — reply на карточку; тикет определяется по `message_id` | `test_manager_reply_reaches_client`, `test_card_message_id_resolves_to_ticket` |
| FR-22 | Reply на постороннее сообщение — понятный отказ | `test_reply_to_unknown_message_explains` |
| FR-23 | Ответ на `new`-тикет сам делает отвечающего ведущим | `test_reply_by_new_ticket_assigns_responder` |
| FR-24 | Ответ менеджера доходит клиенту в личку | `test_manager_reply_reaches_client` |
| FR-25 | Закрытая личка у клиента — понятный отказ, а не падение | `TicketService.relay_from_manager` (ветка `TelegramForbiddenError`) |
| FR-26 | `/close <N>` и кнопка «Закрыть» переводят тикет в `closed` с `closed_at` | `test_close_notifies_and_removes_from_queue`, `test_close_by_button` |
| FR-27 | Закрыть тикет, который ведёт другой менеджер, нельзя | `test_close_by_foreign_manager_is_refused` |
| FR-28 | Новый (не взятый) тикет можно закрыть, не беря в работу | `test_unassigned_ticket_may_be_closed` |
| FR-29 | Закрытый тикет уходит из очереди | `test_close_notifies_and_removes_from_queue` |
| FR-30 | Ответ по закрытому тикету не отправляется | `test_reply_to_closed_ticket_is_refused` |
| FR-31 | `/queue` показывает открытые обращения с состоянием и кнопками «Взять» | `test_queue_lists_open_tickets` |
| FR-32 | Пустая очередь отвечает явно | `test_queue_empty` |
| FR-33 | Команды менеджера не отвечают клиенту, набравшему ту же команду | `test_manager_command_not_answered_to_client` |

## 4. Продолжение переписки

| № | Требование | Проверка |
|---|---|---|
| FR-34 | Свободное сообщение клиента уходит в тикет, если открытый тикет один | `test_plain_message_relays_to_open_ticket` |
| FR-35 | При двух открытых тикетах привязки нет: клиент получает подсказку начать `/start` | `test_two_open_tickets_do_not_guess` |
| FR-36 | Клиент без открытых тикетов получает подсказку начать `/start` | `test_plain_message_without_ticket_asks_for_start` |
| FR-37 | Сообщение клиента в чате менеджеров помечено номером тикета и подписью отправителя | `test_client_message_goes_to_manager_chat` |

## 5. Эксплуатация

| № | Требование | Проверка |
|---|---|---|
| FR-38 | Миграции forward-only, повторный запуск идемпотентен | `test_migrations_are_idempotent` |
| FR-39 | Правка уже применённой миграции обнаруживается (сверка контрольных сумм) | `test_checksum_drift_is_detected` |
| FR-40 | Бот создаёт только схему `support`: `public` не трогает | `test_public_schema_untouched` |
| FR-41 | Модели соответствуют схеме из миграций | `test_models_match_database` |
| FR-42 | Правка категорий и списка менеджеров применяется без перезапуска | `test_reload_applies_change` |
| FR-43 | Битый YAML не роняет бота: остаётся прежняя конфигурация | `test_reload_of_broken_file_keeps_previous` |
| FR-44 | Об одном и том же битом файле сообщается один раз, а не на каждое сообщение | `test_broken_file_reported_once` |
| FR-45 | Опечатка в имени ключа конфига роняет старт с понятным текстом | `test_unknown_key_rejected` |
| FR-46 | Отказы различаются кодами выхода: 2 — конфиг, 3 — база, 4 — токен | `app/__main__.py`, проверено вручную |
| FR-47 | Недоступная база и неверный токен диагностируются до начала приёма апдейтов | там же |

## 6. Нефункциональные

| № | Требование | Как обеспечено |
|---|---|---|
| NFR-1 | Падение в обработчике не останавливает бота | `app/errors.py`: исключение логируется и уходит в Sentry, апдейт считается обработанным |
| NFR-2 | Текст сообщений клиентов не попадает в лог | белый список `ALLOWED_FIELDS` в `app/logging.py`; текст есть только в базе |
| NFR-3 | Ошибки уровня ERROR уходят в Sentry | `LoguruIntegration`; без `SENTRY_DSN` — предупреждение в логе |
| NFR-4 | Токен вычищается из событий Sentry | `_redact` в `app/logging.py`, до отправки и на события, и на breadcrumb |
| NFR-5 | Время берётся только через `Clock` | ruff-правило DTZ и `ManualClock` в тестах |
| NFR-6 | Ошибки конфигурации видит человек, а не стектрейс | `ConfigError` с перечислением полей |
| NFR-7 | Порядок обработки апдейтов: команды менеджера → клиентский сценарий → catch-all последним | `tests/test_wiring.py` |
| NFR-8 | Изменения проходят ruff и mypy (strict) | `uv run ruff check .`, `uv run mypy app` |
