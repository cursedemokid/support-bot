# Ticket Safety and Full Audit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Сделать операции с обращениями конкурентно безопасными, добавить логическое удаление сотрудников и полный административный аудит с XLSX-выгрузкой, одновременно исправив UI, HTML и deep links.

**Architecture:** PostgreSQL обеспечивает инварианты и атомарные переходы, сервисы выполняют бизнес-проверки и пишут аудит в одной транзакции с изменением. Отдельные репозиторий аудита, XLSX-сервис и административный router изолируют хранение, экспорт и UI.

**Tech Stack:** Python 3.12, aiogram 3, SQLAlchemy async, PostgreSQL, asyncpg, openpyxl, pytest, Ruff, mypy.

**Spec:** `docs/superpowers/specs/DemoContact-ticket-safety-and-audit-design.md`

## Global Constraints

- Не логировать токены, байты файлов, Telegram `file_id`, полный текст сообщений, FAQ-ответов и описаний товаров.
- Не логировать навигацию: «Назад», открытие меню, просмотр карточек и списков.
- Bootstrap-администраторы без записи в `staff_members` не добавляются в список передачи.
- Закрытые обращения автоматически не переоткрываются.
- Все изменения выполняются через TDD; миграции остаются forward-only.
- Коммиты создаются от `Developer Developer <DemoRedactedEmail>`.

## Review Focus

- Деактивация одновременно с передачей: обращение остаётся у активного исполнителя либо возвращается в очередь.
- Telegram принял ответ, но запись маршрута упала: транзакция откатывается, история не врёт.
- Очень длинные/не-JSON детали аудита ограничиваются и не ломают действие.
- Большая XLSX использует write-only workbook и сохраняет сортировку.
- Отключённая в БД категория отклоняется, даже если присутствует в YAML.

---

### Task 1: Миграция и модели инвариантов

**Files:**
- Create: `migrations/0010_ticket_safety_and_audit.sql`
- Modify: `app/db/models.py`
- Modify: `tests/test_schema.py`

**Interfaces:**
- Produces: `StaffMember.deleted_at`, `AuditEvent`, индекс `ticket_assignments_one_active_uidx`.
- Consumes: миграции 0001–0008.

- [ ] **Step 1: Написать падающие schema-тесты**

Добавить `audit_events` в `EXPECTED_TABLES`; проверить новые колонки, backfill неактивного сотрудника и `IntegrityError` при втором `TicketAssignment(ended_at=None)` для одного обращения.

- [ ] **Step 2: Запустить RED**

Run: `pytest tests/test_schema.py -q`  
Expected: FAIL — отсутствуют `audit_events`, `deleted_at` и индекс.

- [ ] **Step 3: Добавить миграцию**

```sql
alter table support.staff_members add column if not exists deleted_at timestamptz;
update support.staff_members set deleted_at = updated_at
where not is_active and deleted_at is null;

with ranked as (
  select id, assigned_at,
         row_number() over (partition by ticket_id order by assigned_at desc, id desc) rn
  from support.ticket_assignments where ended_at is null
)
update support.ticket_assignments a set ended_at = ranked.assigned_at
from ranked where a.id = ranked.id and ranked.rn > 1;

create unique index if not exists ticket_assignments_one_active_uidx
on support.ticket_assignments(ticket_id) where ended_at is null;
```

Создать `audit_events` с CHECK для `actor_type`/`result`, `details jsonb not null default '{}'`, индексами по дате, объекту и исполнителю. Внешние ключи не добавлять: аудит переживает логическое удаление объектов.

- [ ] **Step 4: Добавить модели**

```python
class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_at: Mapped[dt.datetime] = mapped_column(TZ)
    actor_type: Mapped[str] = mapped_column(Text)
    actor_telegram_user_id: Mapped[int | None] = mapped_column(BigInteger)
    action: Mapped[str] = mapped_column(Text)
    entity_type: Mapped[str] = mapped_column(Text)
    entity_id: Mapped[str | None] = mapped_column(Text)
    result: Mapped[str] = mapped_column(Text)
    target_telegram_user_id: Mapped[int | None] = mapped_column(BigInteger)
    details: Mapped[dict[str, object]] = mapped_column(JSONB)
    error_code: Mapped[str | None] = mapped_column(Text)
```

Добавить `StaffMember.deleted_at: Mapped[dt.datetime | None]`.

- [ ] **Step 5: Проверить GREEN и коммит**

Run: `pytest tests/test_schema.py -q`  
Expected: PASS.

```bash
git add migrations/0010_ticket_safety_and_audit.sql app/db/models.py tests/test_schema.py
git commit -m "Добавить схему безопасных назначений и аудита"
```

### Task 2: Репозиторий аудита и безопасный XLSX

**Files:**
- Create: `app/repositories/audit.py`
- Create: `app/services/audit_excel.py`
- Create: `tests/test_audit.py`
- Create: `tests/test_audit_excel.py`

**Interfaces:**
- Consumes: `AuditEvent` из Task 1.
- Produces:
  - `record_event(session, *, created_at, actor_type, action, entity_type, result="success", actor_telegram_user_id=None, entity_id=None, target_telegram_user_id=None, details=None, error_code=None) -> AuditEvent`
  - `list_recent(session, limit=30) -> list[AuditEvent]`
  - `list_for_export(session) -> list[AuditEvent]`
  - `build_audit_workbook(events, staff_by_user_id) -> bytes`.

- [ ] **Step 1: Написать падающие тесты репозитория**

Проверить сортировку новых событий сверху, `limit`, JSON-детали и неизменность ранее записанного события.

- [ ] **Step 2: Запустить RED**

Run: `pytest tests/test_audit.py -q`  
Expected: ERROR — модуль `app.repositories.audit` отсутствует.

- [ ] **Step 3: Реализовать репозиторий и санитайзер**

```python
def sanitize_details(value: Mapping[str, object] | None) -> dict[str, object]:
    # Не более 30 ключей; ключ <= 64, строка <= 500.
    # Коллекции становятся ограниченными списками простых значений.
    # Неизвестный объект становится {"type": type(value).__name__}, не repr().

async def record_event(
    session: AsyncSession,
    *,
    created_at: dt.datetime,
    actor_type: str,
    action: str,
    entity_type: str,
    result: str = "success",
    actor_telegram_user_id: int | None = None,
    entity_id: str | None = None,
    target_telegram_user_id: int | None = None,
    details: Mapping[str, object] | None = None,
    error_code: str | None = None,
) -> AuditEvent:
    item = AuditEvent(
        created_at=created_at,
        actor_type=actor_type,
        actor_telegram_user_id=actor_telegram_user_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        result=result,
        target_telegram_user_id=target_telegram_user_id,
        details=sanitize_details(details),
        error_code=error_code,
    )
    session.add(item)
    await session.flush()
    return item
```

`record_event` не делает `commit`.

- [ ] **Step 4: Проверить GREEN репозитория**

Run: `pytest tests/test_audit.py -q`  
Expected: PASS.

- [ ] **Step 5: Написать падающий тест XLSX**

Открыть результат через `load_workbook(BytesIO(data), read_only=True)`; проверить листы `Журнал`/`Легенда`, заголовки, порядок и отсутствие секретного содержимого.

- [ ] **Step 6: Реализовать XLSX**

```python
AUDIT_COLUMNS = (
    "Дата", "Исполнитель", "Роль", "Действие", "Тип объекта",
    "ID объекта", "Результат", "Получатель", "Код ошибки", "Детали",
)

def build_audit_workbook(
    events: Sequence[AuditEvent],
    staff_by_user_id: Mapping[int, StaffMember],
) -> bytes:
    workbook = Workbook(write_only=True)
    journal = workbook.create_sheet("Журнал")
    journal.append(AUDIT_COLUMNS)
    for event in events:
        journal.append(audit_event_row(event, staff_by_user_id))
    legend = workbook.create_sheet("Легенда")
    legend.append(("Код", "Описание"))
    for code, label in sorted(ACTION_LABELS.items()):
        legend.append((code, label))
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()
```

- [ ] **Step 7: Проверить и коммит**

Run: `pytest tests/test_audit.py tests/test_audit_excel.py -q`  
Expected: PASS.

```bash
git add app/repositories/audit.py app/services/audit_excel.py tests/test_audit.py tests/test_audit_excel.py
git commit -m "Добавить хранение и экспорт журнала действий"
```

### Task 3: Атомарный жизненный цикл обращений

**Files:**
- Modify: `app/repositories/tickets.py`
- Modify: `app/services/tickets.py`
- Modify: `app/bot/handlers/manager_commands.py`
- Modify: `tests/test_tickets.py`
- Modify: `tests/test_handlers.py`

**Interfaces:**
- Consumes: `audit_repo.record_event`.
- Produces:
  - `lock_ticket(session, ticket_id) -> Ticket | None`
  - `close_ticket(session, *, ticket_id, closed_at, manager_id) -> Ticket | None`;
  - `transfer_ticket(session, *, ticket_id, from_manager_id, to_manager_id, transferred_at) -> Ticket | None`;
  - `list_open()` возвращает только `new`.

- [ ] **Step 1: Написать RED-тесты правил**

Заменить старый `test_unassigned_ticket_may_be_closed` на отказ. Добавить:

`test_queue_excludes_assigned_ticket`, `test_close_requires_current_assignee`,
`test_transfer_ends_exactly_one_assignment` и
`test_failed_delivery_does_not_persist_reply_or_assignment`.

Для доставки использовать `FakeBot(fail_send=True)`; после rollback проверить историю, состояние и активные назначения.

- [ ] **Step 2: Запустить RED**

Run: `pytest tests/test_tickets.py tests/test_handlers.py -q`  
Expected: FAIL на очереди, закрытии и истории недоставленного ответа.

- [ ] **Step 3: Реализовать атомарные репозиторные операции**

```python
async def lock_ticket(session, ticket_id):
    result = await session.execute(
        select(Ticket).where(Ticket.id == ticket_id).with_for_update()
    )
    return result.scalar_one_or_none()

async def close_ticket(
    session: AsyncSession,
    *,
    ticket_id: int,
    closed_at: dt.datetime,
    manager_id: int,
) -> Ticket | None:
    result = await session.execute(
        update(Ticket)
        .where(Ticket.id == ticket_id,
               Ticket.state == TicketState.ASSIGNED.value,
               Ticket.manager_id == manager_id)
        .values(state=TicketState.CLOSED.value, closed_at=closed_at)
        .returning(Ticket.id)
    )
    # завершить только активное назначение, затем вернуть refreshed Ticket
```

`transfer_ticket` сначала условно меняет строку ticket, затем завершает старое и вставляет новое назначение. Все пути используют один порядок блокировки: сначала `tickets`, затем `ticket_assignments`.

- [ ] **Step 4: Исправить сервис и транзакционные границы**

`TicketService.list_open` вызывает `list_unassigned`. `take/close/transfer/reply` пишут success/rejected события. В `relay_from_manager` доставка выполняется до `messages_repo.add_message`. При `ActionResult(ok=False)` handler вызывает `rollback`, затем записывает отказ/ошибку отдельной транзакцией; при успехе — `commit`.

- [ ] **Step 5: Добавить конкурентные integration-тесты**

Две независимые сессии одновременно вызывают take/transfer/close через `asyncio.gather`; итог: один success, один rejected, один согласованный ticket и не более одного активного назначения.

- [ ] **Step 6: Проверить и коммит**

Run: `pytest tests/test_tickets.py tests/test_handlers.py -q`  
Expected: PASS.

```bash
git add app/repositories/tickets.py app/services/tickets.py app/bot/handlers/manager_commands.py tests/test_tickets.py tests/test_handlers.py
git commit -m "Сделать операции с обращениями атомарными"
```

### Task 4: Логическое удаление сотрудника и возврат обращений

**Files:**
- Modify: `app/repositories/staff.py`
- Modify: `app/repositories/tickets.py`
- Modify: `app/bot/handlers/admin_content.py`
- Create: `tests/test_staff_deactivation.py`

**Interfaces:**
- Produces: `deactivate_member(session, *, member_id, actor_user_id, now) -> tuple[StaffMember | None, list[int]]`.
- Consumes: модели Task 1 и audit Task 2.

- [ ] **Step 1: Написать RED-тесты**

Создать сотрудника с двумя assigned tickets. После деактивации проверить `deleted_at`, `is_active=false`, оба ticket=`new`, пустые manager/assigned_at, завершённые назначения и события `staff.deactivate` + два `ticket.requeue`. Отдельно проверить повторную активацию и гонку деактивации с передачей.

- [ ] **Step 2: Запустить RED**

Run: `pytest tests/test_staff_deactivation.py -q`  
Expected: FAIL — API отсутствует.

- [ ] **Step 3: Реализовать одну транзакционную операцию**

```python
async def deactivate_member(
    session: AsyncSession,
    *,
    member_id: int,
    actor_user_id: int,
    now: dt.datetime,
) -> tuple[StaffMember | None, list[int]]:
    member = await session.scalar(
        select(StaffMember).where(StaffMember.id == member_id).with_for_update()
    )
    if member is None:
        return None, []
    tickets = (
        await tickets_repo.lock_assigned_to(session, member.telegram_user_id)
        if member.telegram_user_id is not None
        else []
    )
    requeued_ticket_ids = [ticket.id for ticket in tickets]
    await tickets_repo.requeue_locked(session, tickets=tickets, ended_at=now)
    member.is_active = False
    member.deleted_at = now
    member.updated_at = now
    return member, requeued_ticket_ids
```

`update_member(is_active=True)` очищает `deleted_at` и пишет `staff.reactivate`.

- [ ] **Step 4: Подключить handler**

`staff_update` использует новый API; ответ callback: `Сотрудник отключён. В очередь возвращено: N.` Все изменения и аудит подтверждаются одним `commit`.

- [ ] **Step 5: Проверить и коммит**

Run: `pytest tests/test_staff_deactivation.py tests/test_handlers.py -q`  
Expected: PASS.

```bash
git add app/repositories/staff.py app/repositories/tickets.py app/bot/handlers/admin_content.py tests/test_staff_deactivation.py
git commit -m "Возвращать обращения при отключении сотрудника"
```

### Task 5: Аудит всех значимых действий

**Files:**
- Modify: `app/services/tickets.py`
- Modify: `app/bot/handlers/admin_content.py`
- Modify: `app/bot/handlers/admin_faq.py`
- Modify: `app/bot/handlers/user_flow.py`
- Modify: `app/errors.py`
- Modify: `app/bot/factory.py`
- Create: `tests/test_audit_coverage.py`
- Modify: `tests/test_errors.py`

**Interfaces:**
- Consumes: `audit_repo.record_event`; `session_factory` и `Clock` для глобальной ошибки.
- Produces: стабильные action-коды из спецификации; `register_error_handler(dispatcher, session_factory, clock)`.

- [ ] **Step 1: Написать параметризованные RED-тесты аудита**

Через реальные сервисы/handlers выполнить по одному действию каждого класса и проверить коды:

```python
EXPECTED_ACTIONS = {
    "staff.invite", "staff.bind", "staff.rename", "staff.role",
    "product.create", "product.update", "product.delete", "product.pdf_download",
    "category.create", "instruction.create",
    "faq.type_create", "faq.type_update", "faq.type_delete",
    "faq.item_create", "faq.item_move", "faq.item_delete",
    "faq.template_download", "faq.import",
    "ticket.create", "ticket.client_message",
}
```

Проверить, что детали не содержат введённый текст, `file_id`, токен или байты. Отдельный тест вызывает невалидный FAQ import и ожидает `faq.import/rejected`.

- [ ] **Step 2: Запустить RED**

Run: `pytest tests/test_audit_coverage.py -q`  
Expected: FAIL — события отсутствуют.

- [ ] **Step 3: Инструментировать успешные мутации**

Перед каждым существующим `session.commit()` мутационного handler добавить `record_event` в ту же транзакцию. `details` содержит только имена изменённых полей, счётчики и безопасные идентификаторы:

```python
await audit_repo.record_event(
    session,
    created_at=clock.now(),
    actor_type="admin",
    actor_telegram_user_id=query.from_user.id,
    action="product.update",
    entity_type="product",
    entity_id=str(product_id),
    details={"fields": [field]},
)
```

TicketService пишет клиентские и сотруднические события. Скачивания записываются перед отправкой файла и подтверждаются отдельным commit.

- [ ] **Step 4: Инструментировать rejected/error**

После `rollback` записать событие в новой транзакции. Ошибки валидации используют стабильный `error_code`, например `duplicate_sku`, `invalid_workbook`, `telegram_delivery_failed`; текст исключения в details не писать.

- [ ] **Step 5: Подключить системные ошибки**

`register_error_handler` замыкает `session_factory` и `clock`; после технического Loguru-события открывает новую сессию, пишет `system.update_error` с update ID и именем класса исключения и коммитит. Ошибка аудита подавляется с отдельным ERROR-логом, чтобы error handler никогда не падал.

- [ ] **Step 6: Проверить GREEN и коммит**

Run: `pytest tests/test_audit_coverage.py tests/test_errors.py tests/test_admin_faq.py tests/test_product_editing.py -q`  
Expected: PASS.

```bash
git add app/services/tickets.py app/bot/handlers/admin_content.py app/bot/handlers/admin_faq.py app/bot/handlers/user_flow.py app/errors.py app/bot/factory.py tests/test_audit_coverage.py tests/test_errors.py
git commit -m "Добавить полный аудит действий бота"
```

### Task 6: Административный журнал и выгрузка

**Files:**
- Create: `app/bot/handlers/admin_audit.py`
- Modify: `app/bot/handlers/__init__.py`
- Modify: `app/bot/keyboards.py`
- Create: `tests/test_admin_audit.py`
- Modify: `tests/test_wiring.py`

**Interfaces:**
- Consumes: `list_recent`, `list_for_export`, `build_audit_workbook`.
- Produces: callbacks `adm:audit`, `admaudit:refresh`, `admaudit:export`; router защищён `IsAdmin()`.

- [ ] **Step 1: Написать RED-тесты доступа и UI**

Администратор видит кнопку и последние 30 событий в обратном порядке. Сотрудник support и клиент не проходят router/filter. Проверить экранирование имени/объекта и наличие кнопок обновления, экспорта, назад.

- [ ] **Step 2: Написать RED-тест экспорта**

Callback экспорта отправляет `BufferedInputFile` с именем `audit-YYYYMMDD-HHMMSS.xlsx`, затем создаёт `audit.export`. Событие экспорта отсутствует в текущем файле, но присутствует в БД.

- [ ] **Step 3: Запустить RED**

Run: `pytest tests/test_admin_audit.py tests/test_wiring.py -q`  
Expected: FAIL — router и callbacks отсутствуют.

- [ ] **Step 4: Реализовать клавиатуру и router**

```python
def audit_keyboard() -> InlineKeyboardMarkup:
    return with_back(
        "admaudit",
        [("refresh", "🔄 Обновить"), ("export", "📥 Скачать Excel")],
        "mgr:home",
    )
```

`admin_audit.py` форматирует до 30 событий с `html.quote`, строит staff map для XLSX и удаляет отправленный документ из UI не требуется: файл остаётся пользовательским артефактом, экран меню по-прежнему заменяется через `UiService`.

- [ ] **Step 5: Подключить router и проверить**

Run: `pytest tests/test_admin_audit.py tests/test_wiring.py -q`  
Expected: PASS.

- [ ] **Step 6: Коммит**

```bash
git add app/bot/handlers/admin_audit.py app/bot/handlers/__init__.py app/bot/keyboards.py tests/test_admin_audit.py tests/test_wiring.py
git commit -m "Добавить просмотр и выгрузку журнала действий"
```

### Task 7: Устойчивый UiService без накопления блоков

**Files:**
- Modify: `app/services/ui.py`
- Modify: `tests/factories.py`
- Create: `tests/test_ui_service.py`

**Interfaces:**
- Produces: прежний публичный `UiService.render`, но с поэтапной фиксацией.
- Consumes: `ui_repo.remember/forget_all/list_messages`.

- [ ] **Step 1: Расширить FakeBot и написать RED**

`FakeBot` получает `fail_on_send_number: int | None`. Тест: старый экран записан, новый render состоит из трёх частей, вторая отправка падает. После ошибки БД не содержит старый экран, содержит первую новую часть; следующий render удаляет её и оставляет только окончательный экран.

- [ ] **Step 2: Добавить тест ошибки commit после Telegram send**

Подменить `ui_repo.remember` так, чтобы он поднял `SQLAlchemyError`; проверить best-effort `delete_message` только что отправленного сообщения.

- [ ] **Step 3: Запустить RED**

Run: `pytest tests/test_ui_service.py -q`  
Expected: FAIL — записи фиксируются только в конце.

- [ ] **Step 4: Реализовать поэтапную фиксацию**

```python
async def clear(
    self,
    session: AsyncSession,
    bot: Bot,
    chat_id: int,
) -> None:
    for saved in await ui_repo.list_messages(session, chat_id):
        with suppress(TelegramBadRequest, TelegramForbiddenError):
            await bot.delete_message(chat_id, saved.message_id)
    await ui_repo.forget_all(session, chat_id)
    await session.commit()

async def _remember_sent(
    self,
    session: AsyncSession,
    bot: Bot,
    *,
    chat_id: int,
    sent: Message,
) -> None:
    try:
        await ui_repo.remember(
            session,
            chat_id=chat_id,
            message_id=sent.message_id,
            created_at=self._clock.now(),
        )
        await session.commit()
    except Exception:
        await session.rollback()
        with suppress(TelegramBadRequest, TelegramForbiddenError):
            await bot.delete_message(chat_id, sent.message_id)
        raise
```

Каждая часть вызывается через `_send_and_remember`; поздняя ошибка не забывает ранние части.

- [ ] **Step 5: Проверить и коммит**

Run: `pytest tests/test_ui_service.py tests/test_handlers.py tests/test_wiring.py -q`  
Expected: PASS.

```bash
git add app/services/ui.py tests/factories.py tests/test_ui_service.py
git commit -m "Сделать замену экранов устойчивой к частичным ошибкам"
```

### Task 8: HTML-безопасность и категории deep link

**Files:**
- Modify: `app/catalog.py`
- Modify: `app/bot/handlers/admin_content.py`
- Modify: `app/bot/handlers/admin_faq.py`
- Modify: `app/bot/handlers/user_flow.py`
- Modify: `app/bot/handlers/manager_commands.py`
- Modify: `tests/test_catalog_view.py`
- Modify: `tests/test_admin_faq.py`
- Modify: `tests/test_handlers.py`

**Interfaces:**
- Produces: безопасные HTML fragments; helper `active_category_for_start(session, config, key) -> Category | CategoryConfig | None`.

- [ ] **Step 1: Написать RED-тесты HTML**

Создать товар, FAQ, категорию и сотрудника со строкой `<b>X</b> & <broken>`; открыть клиентские и административные карточки. Ожидать `&lt;b&gt;X&lt;/b&gt; &amp; &lt;broken&gt;` и отсутствие исходного тега.

- [ ] **Step 2: Написать RED-тесты deep link**

Сценарии: активная категория только в БД принимается; отключённая в БД категория отклоняется даже при наличии в YAML; неизвестная отклоняется; YAML-only категория остаётся fallback.

- [ ] **Step 3: Запустить RED**

Run: `pytest tests/test_catalog_view.py tests/test_admin_faq.py tests/test_handlers.py -q`  
Expected: FAIL на raw HTML и DB-only category.

- [ ] **Step 4: Экранировать на границе форматирования**

Во всех f-string, отправляемых при HTML parse mode, применять `html.quote(str(value))` только к динамическим значениям. Статические `<b>`, `<code>` не экранировать. Репозитории не изменять.

- [ ] **Step 5: Реализовать источник истины deep link**

```python
async def active_category_for_start(session, config, key):
    db_category = await content_repo.category_any_state(session, key)
    if db_category is not None:
        return db_category if db_category.is_active else None
    return category_of(config, key)
```

Добавить `category_any_state`, потому что существующий active-only запрос не отличает отключённую запись от отсутствующей.

- [ ] **Step 6: Проверить и коммит**

Run: `pytest tests/test_catalog_view.py tests/test_admin_faq.py tests/test_handlers.py -q`  
Expected: PASS.

```bash
git add app/catalog.py app/bot/handlers/admin_content.py app/bot/handlers/admin_faq.py app/bot/handlers/user_flow.py app/bot/handlers/manager_commands.py app/repositories/content.py tests/test_catalog_view.py tests/test_admin_faq.py tests/test_handlers.py
git commit -m "Защитить HTML и deep links категорий"
```

### Task 9: Полная интеграционная проверка и эксплуатация

**Files:**
- Modify: `tests/test_wiring.py`
- Modify: `README.md`
- Modify: `docs/superpowers/specs/DemoContact-ticket-safety-and-audit-design.md` (статус `реализовано` после успешной проверки)

**Interfaces:**
- Consumes: все интерфейсы Tasks 1–8.
- Produces: проверенный контейнер и документацию администратора.

- [ ] **Step 1: Добавить сквозной RED-тест**

Через dispatcher: создать обращение клиентом, взять первым сотрудником, убедиться, что второй его не видит, ответить, передать, деактивировать получателя, убедиться в возврате в очередь, взять снова и закрыть. Администратор открывает аудит и скачивает XLSX; тест проверяет последовательность action-кодов.

- [ ] **Step 2: Запустить сквозной тест**

Run: `pytest tests/test_wiring.py -q`  
Expected: PASS после Tasks 1–8; если нет — исправлять только обнаруженную интеграционную несовместимость с отдельным RED-тестом.

- [ ] **Step 3: Обновить документацию**

Описать раздел «Журнал действий», логическое отключение сотрудника, возврат обращений и ограничения аудита. После всех проверок поменять статус спецификации на `реализовано`.

- [ ] **Step 4: Запустить статические проверки**

Run: `ruff check .`  
Expected: PASS.

Run: `mypy app`  
Expected: PASS.

- [ ] **Step 5: Запустить полный набор**

Run: `pytest -q`  
Expected: все тесты PASS, ни одного неожиданного skip кроме тестов, которым явно нужна отсутствующая внешняя БД.

- [ ] **Step 6: Проверить миграции и контейнер**

Run: `docker compose run --rm bot python -m app.migrate`  
Expected: миграция 0010 применена либо база уже актуальна.

Run: `docker compose up -d --build && docker compose ps && docker compose logs --tail=100 bot`  
Expected: контейнер `Up`, в логах нет traceback/ERROR при старте.

- [ ] **Step 7: Финальный коммит**

```bash
git add tests/test_wiring.py README.md docs/superpowers/specs/DemoContact-ticket-safety-and-audit-design.md
git commit -m "Проверить полный сценарий обращений и аудита"
```

После коммита выполнить whole-branch code review по диапазону от `787f419` до `HEAD`; Critical/Important исправить TDD-циклом, Minor перечислить пользователю.
