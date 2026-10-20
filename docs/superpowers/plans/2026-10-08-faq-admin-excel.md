# FAQ Admin and Excel Import Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Добавить раздельное управление типами и вопросами FAQ, безопасное сохранение вопросов после удаления типа и атомарный экспорт/импорт FAQ через документированный Excel-шаблон.

**Architecture:** Telegram-интерфейс FAQ выделяется из большого `admin_content.py` в отдельный административный роутер. Репозиторий владеет SQL-операциями и транзакционной блокировкой ID, сервис `faq_excel.py` владеет форматом XLSX, полной валидацией и применением импорта, а обработчики только переключают FSM, отправляют документы и показывают результат. Вопрос без типа хранится как `faq_items.type_id = NULL`, скрывается от клиента и остаётся доступным администратору.

**Tech Stack:** Python 3.12, aiogram 3, SQLAlchemy 2 async, PostgreSQL, openpyxl, pytest, pytest-asyncio, Ruff, mypy.

**Spec:** `docs/superpowers/specs/2026-10-08-faq-admin-excel-design.md`

## Global Constraints

- Постоянный ID нового типа назначает бот как `MAX(faq_types.id) + 1`; сотрудник его не придумывает.
- Значения столбца «Действие» — только `СОХРАНИТЬ` и `УДАЛИТЬ`; их можно выбирать из списка или вставлять текстом.
- Отсутствующая в файле строка не удаляется и не изменяется.
- Любая ошибка отменяет весь импорт; частичное применение запрещено.
- Новый вопрос нельзя создать без активного типа; существующий вопрос может находиться в разделе «Без типа».
- Удаление типа логическое и переводит связанные вопросы в `type_id = NULL`.
- Клиент видит только активные вопросы активных типов.
- Каждый Telegram-экран заменяет предыдущий через `UiService` и содержит корректный возврат назад.
- XLSX ограничен 5 МБ, 500 строками типов и 2 000 строками вопросов.
- Старые `instruction_sections` и их данные остаются в базе; из интерфейса удаляется только неиспользуемая кнопка.

## Review Focus

- Параллельные добавления типов: второй импорт не должен получить занятый `MAX(id) + 1`, а обязан отклониться до записи данных.
- Смешанный файл с удаляемым типом и новым вопросом на этот тип: импорт отклоняется целиком с указанием листа и строки.
- Значение действия с пробелами, другим регистром или похожими буквами: принимаются только нормализованные `СОХРАНИТЬ`/`УДАЛИТЬ`, неизвестное значение блокирует импорт.
- Вопросы удалённого типа: остаются активными, получают `NULL`, скрываются от клиента и появляются в «Без типа».
- Повторная загрузка файла: строки с заполненными ID обновляются, не дублируются; новые вопросы с пустым ID создаются повторно и это явно объяснено в инструкции.

---

### Task 1: Схема и репозиторий вопросов без типа

**Files:**
- Create: `migrations/0008_faq_orphans.sql`
- Modify: `app/db/models.py`
- Modify: `app/repositories/faq.py`
- Modify: `tests/test_schema.py`
- Modify: `tests/test_faq.py`

**Interfaces:**
- Produces: `next_type_id(session) -> int`, `create_type(session, *, title, sort_order=0, type_id=None) -> FaqType`, `disable_type(session, type_id) -> int`, `orphan_items(session) -> list[FaqItem]`, `disable_item(session, item_id) -> bool`, `assign_item(session, *, item_id, type_id) -> bool`.
- Consumes: существующие модели `FaqType`, `FaqItem` и транзакцию `AsyncSession`.

- [ ] **Step 1: Написать падающие тесты миграции и поведения репозитория**

```python
async def test_disabling_type_keeps_questions_as_orphans(session_factory):
    async with session_factory() as session, session.begin():
        kind = await faq.create_type(session, title="Доставка", type_id=41)
        item = await faq.create_item(
            session, type_id=kind.id, question="Когда?", answer="Завтра"
        )
        orphaned = await faq.disable_type(session, kind.id)
        assert orphaned == 1

    async with session_factory() as session:
        stored = await session.get(FaqItem, item.id)
        assert stored is not None and stored.is_active and stored.type_id is None
        assert [row.id for row in await faq.orphan_items(session)] == [item.id]
```

Добавить в `test_schema.py` проверку, что `faq_items.type_id` допускает `NULL` и внешний ключ использует `ON DELETE SET NULL`.

- [ ] **Step 2: Запустить тесты и подтвердить ожидаемое падение**

Run: `TEST_DATABASE_URL="$TEST_DATABASE_URL" uv run pytest tests/test_schema.py tests/test_faq.py -q`

Expected: FAIL — текущий столбец `type_id` имеет `NOT NULL`, а вопросы не переводятся в сироты.

- [ ] **Step 3: Добавить forward-only миграцию**

```sql
alter table support.faq_items
    drop constraint if exists faq_items_type_id_fkey;
alter table support.faq_items
    alter column type_id drop not null;
alter table support.faq_items
    add constraint faq_items_type_id_fkey
    foreign key (type_id) references support.faq_types(id) on delete set null;
create index if not exists faq_items_orphan_idx
    on support.faq_items (sort_order, id)
    where is_active and type_id is null;
```

- [ ] **Step 4: Обновить модель и реализовать операции репозитория**

```python
async def next_type_id(session: AsyncSession) -> int:
    await session.execute(text("select pg_advisory_xact_lock(716_201_001)"))
    value = await session.scalar(select(func.coalesce(func.max(FaqType.id), 0) + 1))
    return int(value or 1)


async def disable_type(session: AsyncSession, type_id: int) -> int:
    value = await session.get(FaqType, type_id)
    if value is None or not value.is_active:
        return -1
    value.is_active = False
    result = await session.execute(
        update(FaqItem)
        .where(FaqItem.type_id == type_id, FaqItem.is_active)
        .values(type_id=None)
    )
    await session.flush()
    return int(result.rowcount or 0)
```

Тип `FaqItem.type_id` изменить на `Mapped[int | None]`. `items()` оставить только для конкретного типа, а `orphan_items()` сделать отдельным явным запросом.

- [ ] **Step 5: Проверить генерацию ID, удаление вопроса и переназначение**

Добавить тесты на два последовательных `create_type()` (`max + 1`), `disable_item()` и отказ `assign_item()` при неактивном/несуществующем типе.

Run: `TEST_DATABASE_URL="$TEST_DATABASE_URL" uv run pytest tests/test_schema.py tests/test_faq.py -q`

Expected: PASS.

- [ ] **Step 6: Зафиксировать задачу**

```bash
git add migrations/0008_faq_orphans.sql app/db/models.py app/repositories/faq.py tests/test_schema.py tests/test_faq.py
git commit -m "feat: preserve FAQ questions after type deletion"
```

### Task 2: Раздельные административные списки типов и вопросов

**Files:**
- Create: `app/bot/handlers/admin_faq.py`
- Modify: `app/bot/handlers/admin_content.py`
- Modify: `app/bot/handlers/__init__.py`
- Modify: `app/bot/keyboards.py`
- Modify: `tests/test_callbacks.py`
- Create: `tests/test_admin_faq.py`

**Interfaces:**
- Consumes: репозиторные функции Task 1 и `UiService.render()`.
- Produces: router `admin_faq.router`; callback namespaces `admfaq:*`, `admfaqtype:*`, `admfaqquestions:*`, `admfaqitem:*`; FSM `AdminFaqFlow`.

- [ ] **Step 1: Написать тесты клавиатур и маршрутов**

```python
def test_faq_admin_root_has_separate_lists():
    markup = faq_admin_menu()
    assert callbacks(markup) == [
        "admfaq:types",
        "admfaq:questions",
        "admfaq:download",
        "admfaq:upload",
        "adm:menu",
    ]


async def test_question_type_picker_puts_orphans_first(session_factory):
    # Создать активный тип и активный вопрос с type_id=None.
    # Открыть admfaq:questions через настоящий handler/FakeBot.
    assert bot.buttons(bot.sent[-1])[:2] == ["Без типа", "Доставка"]
```

Добавить отдельные тесты: карточка типа содержит удаление; карточка вопроса содержит перенос и удаление; в вопросном маршруте нет действия удаления типа; кнопка «Инструкции» отсутствует в `admin_menu()`.

- [ ] **Step 2: Запустить тесты и подтвердить падение**

Run: `uv run pytest tests/test_callbacks.py tests/test_admin_faq.py tests/test_catalog_view.py -q`

Expected: FAIL — отдельного роутера и новых меню ещё нет.

- [ ] **Step 3: Удалить неиспользуемую кнопку и выделить FAQ-роутер**

В `admin_menu()` оставить `Товары`, `Категории`, `Вопросы и ответы`, `Назад`. Перенести FAQ FSM и обработчики из `admin_content.py` в `admin_faq.py`, повесить на router `IsAdmin()` и зарегистрировать его до клиентского роутера:

```python
root.include_router(admin_faq.router)
root.include_router(admin_content.router)
root.include_router(manager_commands.router)
```

- [ ] **Step 4: Реализовать список типов и операции типа**

`admfaq:types` показывает «Добавить тип» и активные типы. Добавление последовательно спрашивает название и целочисленный порядок; ID получает через `faq_repo.create_type()`. `admfaqtype:{id}` показывает карточку. `admfaqtypedel:{id}` запрашивает подтверждение, вызывает `disable_type()`, показывает количество перемещённых в «Без типа» вопросов и возвращает список типов.

- [ ] **Step 5: Реализовать список вопросов, сирот и карточку вопроса**

`admfaq:questions` показывает активные типы и условный callback `admfaqquestions:orphan`. Выбор типа показывает его вопросы и «Добавить вопрос». Карточка `admfaqitem:{id}` показывает вопрос/ответ и кнопки `Перенести в тип`, `Удалить`, `Назад`. Перенос открывает только активные типы; удаление типа в этом сценарии отсутствует.

- [ ] **Step 6: Сделать возвраты одноэкранными и очищать FSM**

Каждый list/card handler принимает `state: FSMContext`, вызывает `state.clear()` перед отрисовкой и использует `UiService`. После добавления, переноса или удаления сразу отрисовывается соответствующий список, без отдельного накопленного сообщения об успехе.

- [ ] **Step 7: Запустить тесты роутера**

Run: `uv run pytest tests/test_callbacks.py tests/test_admin_faq.py tests/test_catalog_view.py -q`

Expected: PASS.

- [ ] **Step 8: Зафиксировать задачу**

```bash
git add app/bot/handlers/admin_faq.py app/bot/handlers/admin_content.py app/bot/handlers/__init__.py app/bot/keyboards.py tests/test_callbacks.py tests/test_admin_faq.py tests/test_catalog_view.py
git commit -m "feat: split FAQ type and question administration"
```

### Task 3: Чистая модель и валидатор Excel-импорта

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Create: `app/services/faq_excel.py`
- Create: `tests/test_faq_excel.py`

**Interfaces:**
- Produces: `FaqTypeRow`, `FaqQuestionRow`, `FaqWorkbookData`, `FaqWorkbookError`, `parse_faq_workbook(payload: bytes) -> FaqWorkbookData`.
- Consumes: `.xlsx` bytes; на этом этапе не обращается к Telegram или базе.

- [ ] **Step 1: Добавить зависимость openpyxl**

Run: `uv add 'openpyxl>=3.1,<4'`

Expected: `pyproject.toml` и `uv.lock` содержат зафиксированную совместимую версию.

- [ ] **Step 2: Написать падающие тесты парсинга и валидации**

```python
def test_parser_accepts_copy_pasted_actions_and_integer_links():
    payload = workbook_bytes(
        types=[[8, "Доставка", 10, "СОХРАНИТЬ"]],
        questions=[[None, 8, "Когда?", "Завтра", 20, "СОХРАНИТЬ"]],
    )
    parsed = parse_faq_workbook(payload)
    assert parsed.types[0].id == 8
    assert parsed.questions[0].type_id == 8


@pytest.mark.parametrize("action", ["", "ДОБАВИТЬ", "DELETE", "СОХРАНИТЬ "])
def test_parser_rejects_unknown_action_with_sheet_and_row(action):
    with pytest.raises(FaqWorkbookError, match=r"Типы.*строка 2.*Действие"):
        parse_faq_workbook(workbook_bytes(types=[[8, "Доставка", 10, action]]))
```

Добавить тесты на: отсутствующий лист/столбец; float/string ID; duplicate ID; лимиты строк; пустой текст; превышение длины; формулу и внешнюю ссылку в значимой ячейке; новый вопрос без типа; ссылку на неизвестный тип; вопрос на тип с `УДАЛИТЬ`; размер более 5 МБ.

- [ ] **Step 3: Запустить тесты и подтвердить падение**

Run: `uv run pytest tests/test_faq_excel.py -q`

Expected: FAIL — модуль `faq_excel` отсутствует.

- [ ] **Step 4: Реализовать типы данных и диагностические ошибки**

```python
SAVE = "СОХРАНИТЬ"
DELETE = "УДАЛИТЬ"

@dataclass(frozen=True, slots=True)
class FaqWorkbookError(ValueError):
    sheet: str
    row: int | None
    column: str | None
    detail: str

    def __str__(self) -> str:
        location = f"Лист «{self.sheet}»"
        if self.row is not None:
            location += f", строка {self.row}"
        if self.column is not None:
            location += f", столбец «{self.column}»"
        return f"{location}: {self.detail}"
```

`FaqTypeRow.id` — положительный `int`; `FaqQuestionRow.id` — `int | None`; `type_id` — `int | None` только для уже существующей строки-сироты.

- [ ] **Step 5: Реализовать строгий parser**

Открывать через `load_workbook(BytesIO(payload), read_only=True, data_only=False, keep_links=False)`. Требовать листы и заголовки из спецификации. Пропускать подготовленные строки типов, где заполнен только `ID типа`, а название, порядок и действие пусты. Если одна из таких строк использована, все предыдущие подготовленные ID также должны быть использованы. Запрещать значения, начинающиеся с `=`, и типы ячеек `f`/`e`. Не исправлять неизвестные действия автоматически; `strip()` разрешён для определения пустой ячейки, а действие должно после нормализации регистра точно совпасть с константой и не содержать внешних пробелов.

- [ ] **Step 6: Запустить parser-тесты**

Run: `uv run pytest tests/test_faq_excel.py -q`

Expected: PASS.

- [ ] **Step 7: Зафиксировать задачу**

```bash
git add pyproject.toml uv.lock app/services/faq_excel.py tests/test_faq_excel.py
git commit -m "feat: validate FAQ Excel workbooks"
```

### Task 4: Генератор Excel-шаблона с инструкцией и подготовленными ID

**Files:**
- Modify: `app/services/faq_excel.py`
- Modify: `tests/test_faq_excel.py`

**Interfaces:**
- Produces: `build_faq_workbook(*, types, questions, next_type_id, new_type_slots=50) -> bytes`.
- Consumes: текущие типы, все активные вопросы, включая сирот, и следующий свободный ID из Task 1.

- [ ] **Step 1: Написать падающий round-trip тест шаблона**

```python
def test_export_contains_instruction_current_data_and_prepared_type_ids():
    payload = build_faq_workbook(
        types=[FaqType(id=7, title="Доставка", sort_order=10, is_active=True)],
        questions=[FaqItem(id=12, type_id=7, question="Когда?", answer="Завтра", sort_order=5, is_active=True)],
        next_type_id=8,
        new_type_slots=3,
    )
    book = load_workbook(BytesIO(payload), data_only=True)
    assert book.sheetnames == ["Инструкция", "Типы", "Вопросы"]
    assert [book["Типы"][f"A{row}"].value for row in range(3, 6)] == [8, 9, 10]
    assert book["Инструкция"]["A1"].value == "Как заполнить шаблон FAQ"
```

Добавить тесты на точные заголовки, текущие данные, строки сирот с пустым `ID типа`, action `СОХРАНИТЬ`, dropdown `СОХРАНИТЬ,УДАЛИТЬ`, freeze panes, filters и легенду ID.

- [ ] **Step 2: Запустить тест и подтвердить падение**

Run: `uv run pytest tests/test_faq_excel.py -q`

Expected: FAIL — генератор отсутствует.

- [ ] **Step 3: Реализовать workbook**

Создать три листа без дополнительного пустого листа. На `Инструкция` записать:

```text
1. Не переименовывайте листы и столбцы.
2. СОХРАНИТЬ — добавить или обновить строку.
3. УДАЛИТЬ — удалить строку; отсутствие строки ничего не удаляет.
4. Для нового типа используйте один из подготовленных ID на листе «Типы».
5. Этот ID можно сразу вставить в «ID типа» нового вопроса.
6. Новый вопрос оставляйте без «ID вопроса»; после загрузки скачайте свежий шаблон.
7. Вопрос без типа не показывается клиентам.
```

Строки текущих данных и подготовленные ID записывать обычными значениями, не формулами. Добавить табличные фильтры, `freeze_panes = "A2"`, перенос текста, разумные ширины столбцов и проверку данных списка для `Действие`. Не использовать макросы, внешние ссылки и скрытую бизнес-логику.

- [ ] **Step 4: Проверить обратный разбор экспортированного файла**

Экспортированный файл после заполнения одной подготовленной строки и одного нового вопроса должен разбираться `parse_faq_workbook()` без специального режима.

Run: `uv run pytest tests/test_faq_excel.py -q`

Expected: PASS.

- [ ] **Step 5: Зафиксировать задачу**

```bash
git add app/services/faq_excel.py tests/test_faq_excel.py
git commit -m "feat: export documented FAQ Excel template"
```

### Task 5: Атомарное применение Excel к базе

**Files:**
- Modify: `app/services/faq_excel.py`
- Modify: `app/repositories/faq.py`
- Modify: `tests/test_faq_excel.py`
- Modify: `tests/test_faq.py`

**Interfaces:**
- Produces: `apply_faq_workbook(session, data) -> FaqImportSummary`.
- Consumes: валидированный `FaqWorkbookData`; требует открытую транзакцию SQLAlchemy.

- [ ] **Step 1: Написать интеграционные тесты атомарного применения**

```python
async def test_import_creates_type_and_question_in_one_transaction(session_factory):
    data = FaqWorkbookData(
        types=(FaqTypeRow(8, "Гарантия", 20, SAVE, row=2),),
        questions=(FaqQuestionRow(None, 8, "Срок?", "Один год", 10, SAVE, row=2),),
    )
    async with session_factory() as session, session.begin():
        summary = await apply_faq_workbook(session, data)
    assert summary.created_types == 1
    assert summary.created_questions == 1
```

Добавить тесты: обновление существующих строк; удаление вопроса; удаление типа и orphan count; отсутствующие строки не меняются; неизвестный ID существующей записи; занятый подготовленный ID; повтор заполненного файла не создаёт дубль; ошибка последней строки откатывает первые изменения.

- [ ] **Step 2: Запустить тесты и подтвердить падение**

Run: `TEST_DATABASE_URL="$TEST_DATABASE_URL" uv run pytest tests/test_faq.py tests/test_faq_excel.py -q`

Expected: FAIL — функция применения отсутствует.

- [ ] **Step 3: Реализовать блокировку и сверку подготовленных ID**

В начале `apply_faq_workbook()` вызвать тот же `pg_advisory_xact_lock`, затем получить актуальный `next_type_id`. Новые ID из файла обязаны составлять начало последовательности `range(next_type_id, next_type_id + new_count)` без дыр и повторов. Несовпадение поднимает `FaqWorkbookError(sheet="Типы", row=first_new_row, column="ID типа", detail="ID уже изменились; скачайте свежий шаблон")`.

- [ ] **Step 4: Применить операции в детерминированном порядке**

Порядок внутри одной транзакции:

1. Проверить все ссылки и конфликты с актуальной базой.
2. Создать/обновить типы с `СОХРАНИТЬ`.
3. Создать/обновить/перенести вопросы с `СОХРАНИТЬ`.
4. Логически удалить вопросы с `УДАЛИТЬ`.
5. Логически удалить типы с `УДАЛИТЬ` и обнулить `type_id` оставшихся активных вопросов.
6. Один `flush`; commit остаётся ответственностью handler/middleware.

`FaqImportSummary` хранит `created_types`, `updated_types`, `deleted_types`, `created_questions`, `updated_questions`, `deleted_questions`, `orphaned_questions`.

- [ ] **Step 5: Проверить транзакционность и конкурентный конфликт**

Run: `TEST_DATABASE_URL="$TEST_DATABASE_URL" uv run pytest tests/test_faq.py tests/test_faq_excel.py -q`

Expected: PASS, включая два конкурентных импорта, где только первый использует зарезервированную последовательность.

- [ ] **Step 6: Зафиксировать задачу**

```bash
git add app/services/faq_excel.py app/repositories/faq.py tests/test_faq_excel.py tests/test_faq.py
git commit -m "feat: apply FAQ Excel imports atomically"
```

### Task 6: Скачивание и загрузка Excel через Telegram

**Files:**
- Modify: `app/bot/handlers/admin_faq.py`
- Modify: `app/bot/keyboards.py`
- Modify: `tests/factories.py`
- Modify: `tests/test_admin_faq.py`
- Modify: `tests/test_wiring.py`

**Interfaces:**
- Consumes: `build_faq_workbook`, `parse_faq_workbook`, `apply_faq_workbook`.
- Produces: callbacks `admfaq:download`, `admfaq:upload`; FSM state `AdminFaqFlow.excel_upload`.
- Produces test helpers: `admin_callback(data: str, user_id: int = MANAGER_ID) -> CallbackQuery`, `admin_fsm() -> FSMContext`, `document_message(payload: bytes, *, filename: str, mime_type: str) -> Message`, `broken_workbook_bytes() -> bytes`.

- [ ] **Step 1: Расширить FakeBot поддержкой документов и написать падающие handler-тесты**

```python
async def test_download_sends_xlsx_with_current_faq(session_factory, clock):
    bot = FakeBot()
    query = admin_callback("admfaq:download").as_(cast(Bot, bot))
    async with session_factory() as session:
        await admin_faq.download_excel(query, session, cast(Bot, bot), clock)
    document = bot.documents_to(ADMIN_ID)[-1]
    assert document.chat.id == MANAGER_ID
    assert document.filename == "faq-template.xlsx"


async def test_invalid_upload_keeps_database_unchanged(session_factory, clock):
    bot = FakeBot()
    state = admin_fsm()
    message = document_message(
        broken_workbook_bytes(),
        filename="faq-template.xlsx",
        mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ).as_(cast(Bot, bot))
    async with session_factory() as session:
        original_types = [row.title for row in await faq.types(session)]
        await admin_faq.upload_excel(message, state, session, cast(Bot, bot), clock)
        stored_types = [row.title for row in await faq.types(session)]
    assert "Лист «Вопросы», строка 4" in bot.texts[-1]
    assert stored_types == original_types
```

Добавить тесты MIME/расширения, лимита 5 МБ, успешного отчёта, очистки FSM, кнопки назад и замены экранов.

- [ ] **Step 2: Запустить handler-тесты и подтвердить падение**

Run: `uv run pytest tests/test_admin_faq.py tests/test_wiring.py -q`

Expected: FAIL — обработчики и отправка документа отсутствуют.

- [ ] **Step 3: Реализовать скачивание шаблона**

Handler загружает активные типы и активные вопросы, включая вопросы без типа, получает `next_type_id`, строит bytes и отправляет `BufferedInputFile(payload, filename="faq-template.xlsx")`. После отправки снова отрисовывает FAQ-меню, чтобы интерфейс не зависел от отдельного документа.

- [ ] **Step 4: Реализовать состояние загрузки**

`admfaq:upload` очищает FSM, ставит `AdminFaqFlow.excel_upload` и показывает обязательную краткую подсказку со ссылкой-кнопкой на скачивание свежего шаблона. Message-handler принимает документ `.xlsx`, скачивает bytes через Bot API, проверяет размер до парсинга, запускает parse/apply, делает commit только после успеха и rollback при `FaqWorkbookError`, `BadZipFile` или нарушении ограничений.

- [ ] **Step 5: Показать понятный результат**

Успех:

```text
FAQ обновлён.
Типы: добавлено 2, изменено 1, удалено 0.
Вопросы: добавлено 5, изменено 3, удалено 1.
Без типа: 4.
```

Ошибка содержит причину и максимум первые 20 проблем, без traceback и без частичного результата. После ошибки FSM остаётся в состоянии загрузки, чтобы можно было прислать исправленный файл; «Назад» очищает состояние.

- [ ] **Step 6: Запустить handler и wiring тесты**

Run: `uv run pytest tests/test_admin_faq.py tests/test_wiring.py -q`

Expected: PASS.

- [ ] **Step 7: Зафиксировать задачу**

```bash
git add app/bot/handlers/admin_faq.py app/bot/keyboards.py tests/factories.py tests/test_admin_faq.py tests/test_wiring.py
git commit -m "feat: manage FAQ through Excel in Telegram"
```

### Task 7: Клиентская видимость, документация и выпуск

**Files:**
- Modify: `app/bot/handlers/user_flow.py`
- Modify: `tests/test_wiring.py`
- Modify: `README.md`
- Modify: `docs/manager-guide.md`

**Interfaces:**
- Consumes: правила видимости Task 1 и административные/Excel-сценарии Tasks 2–6.
- Produces: документированный и проверенный сквозной выпуск.

- [ ] **Step 1: Написать сквозной тест клиентской видимости**

Создать тип с двумя вопросами, удалить тип административным handler, пройти клиентский `sup:faq` и подтвердить отсутствие типа и обоих вопросов. Затем назначить один вопрос новому активному типу и подтвердить, что клиент видит только переназначенный вопрос.

Run: `TEST_DATABASE_URL="$TEST_DATABASE_URL" uv run pytest tests/test_wiring.py -q`

Expected: FAIL до окончательной фильтрации/переноса, затем PASS.

- [ ] **Step 2: Обновить документацию администратора**

В `docs/manager-guide.md` описать отдельные списки, «Без типа», перенос вопроса, скачивание свежего шаблона, точные значения `СОХРАНИТЬ`/`УДАЛИТЬ`, правило пустого `ID вопроса`, подготовленные ID типов, лимиты и атомарность. В README добавить `openpyxl` в описание зависимостей и команду миграции.

- [ ] **Step 3: Выполнить полную проверку**

```bash
UV_CACHE_DIR=/tmp/support-bot/.uv-cache uv run ruff format --check app tests
UV_CACHE_DIR=/tmp/support-bot/.uv-cache uv run ruff check app tests
UV_CACHE_DIR=/tmp/support-bot/.uv-cache uv run mypy app
test -n "$TEST_DATABASE_URL"
UV_CACHE_DIR=/tmp/support-bot/.uv-cache uv run pytest -q
docker compose run --rm migrate --dry-run
docker compose build bot
```

Expected: все команды завершаются с кодом 0; интеграционные тесты не пропущены, миграция `0008` применяется после `0007`, образ собирается.

- [ ] **Step 4: Провести ручную дымовую проверку в тестовом боте**

Проверить: отдельные списки; удаление типа; «Без типа»; перенос и удаление вопроса; скачивание читаемого XLSX; добавление типа и вопроса одним файлом; отказ повреждённого файла без изменений; отсутствие накопления сообщений.

- [ ] **Step 5: Зафиксировать документацию и выпуск**

```bash
git add app/bot/handlers/user_flow.py tests/test_wiring.py README.md docs/manager-guide.md
git commit -m "docs: document FAQ Excel workflow"
```
