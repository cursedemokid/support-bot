"""Административные списки типов и вопросов FAQ."""

from __future__ import annotations

from html import escape

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardMarkup, Message
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.filters.access import IsAdmin
from app.bot.keyboards import (
    back_keyboard,
    faq_admin_menu,
    faq_item_card_keyboard,
    faq_item_move_keyboard,
    faq_question_types_keyboard,
    faq_questions_keyboard,
    faq_type_card_keyboard,
    faq_types_keyboard,
    faq_upload_keyboard,
    with_back,
)
from app.clock import Clock
from app.db.models import FaqItem, FaqType
from app.repositories import faq as faq_repo
from app.services.faq_excel import (
    MAX_FILE_SIZE,
    FaqWorkbookError,
    apply_faq_workbook,
    build_faq_workbook,
    format_import_summary,
    parse_faq_workbook,
)
from app.services.ui import UiService

router = Router(name="admin_faq")
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())


class AdminFaqFlow(StatesGroup):
    type_title = State()
    type_order = State()
    question = State()
    answer = State()
    excel_upload = State()


async def _replace_after_input(
    message: Message,
    session: AsyncSession,
    bot: Bot,
    clock: Clock,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup,
) -> None:
    ui = UiService(clock)
    await ui.remove_incoming(bot, message.chat.id, message.message_id)
    await ui.render(
        session,
        bot,
        chat_id=message.chat.id,
        parts=[text],
        reply_markup=reply_markup,
    )


async def _render_types(
    session: AsyncSession,
    bot: Bot,
    clock: Clock,
    *,
    chat_id: int,
    heading: str = "Типы FAQ:",
) -> None:
    rows = await faq_repo.types(session)
    await UiService(clock).render(
        session,
        bot,
        chat_id=chat_id,
        parts=[heading],
        reply_markup=faq_types_keyboard([(row.id, row.title) for row in rows]),
    )


async def _render_question_types(
    session: AsyncSession,
    bot: Bot,
    clock: Clock,
    *,
    chat_id: int,
) -> None:
    rows = await faq_repo.types(session)
    orphans = await faq_repo.orphan_items(session)
    await UiService(clock).render(
        session,
        bot,
        chat_id=chat_id,
        parts=["Выберите тип, чтобы посмотреть вопросы:"],
        reply_markup=faq_question_types_keyboard(
            [(row.id, row.title) for row in rows], has_orphans=bool(orphans)
        ),
    )


async def _type_by_id(session: AsyncSession, type_id: int) -> FaqType | None:
    return next((row for row in await faq_repo.types(session) if row.id == type_id), None)


async def _render_questions(
    session: AsyncSession,
    bot: Bot,
    clock: Clock,
    *,
    chat_id: int,
    type_id: int | None,
    heading: str | None = None,
) -> None:
    if type_id is None:
        rows = await faq_repo.orphan_items(session)
        title = "Вопросы без типа:"
    else:
        faq_type = await _type_by_id(session, type_id)
        if faq_type is None:
            await _render_question_types(session, bot, clock, chat_id=chat_id)
            return
        rows = await faq_repo.items(session, type_id)
        title = f"Вопросы: {escape(faq_type.title)}"
    await UiService(clock).render(
        session,
        bot,
        chat_id=chat_id,
        parts=[heading or title],
        reply_markup=faq_questions_keyboard(
            [(row.id, row.question) for row in rows], type_id=type_id
        ),
    )


def _one_int(data: str | None) -> int | None:
    try:
        return int((data or "").rsplit(":", 1)[1])
    except (IndexError, ValueError):
        return None


@router.callback_query(F.data == "adm:faq")
async def faq_menu(
    query: CallbackQuery,
    session: AsyncSession,
    bot: Bot,
    clock: Clock,
    state: FSMContext,
) -> None:
    await state.clear()
    await query.answer()
    await UiService(clock).render(
        session,
        bot,
        chat_id=query.from_user.id,
        parts=["Вопросы и ответы:"],
        reply_markup=faq_admin_menu(),
    )


@router.callback_query(F.data == "admfaq:types")
async def show_types(
    query: CallbackQuery,
    session: AsyncSession,
    bot: Bot,
    clock: Clock,
    state: FSMContext,
) -> None:
    await state.clear()
    await query.answer()
    await _render_types(session, bot, clock, chat_id=query.from_user.id)


@router.callback_query(F.data == "admfaqtype:add")
async def add_type_start(
    query: CallbackQuery,
    session: AsyncSession,
    bot: Bot,
    clock: Clock,
    state: FSMContext,
) -> None:
    await state.clear()
    await state.set_state(AdminFaqFlow.type_title)
    await query.answer()
    await UiService(clock).render(
        session,
        bot,
        chat_id=query.from_user.id,
        parts=["Введите название нового типа FAQ."],
        reply_markup=back_keyboard("admfaq:types"),
    )


@router.message(AdminFaqFlow.type_title, F.text)
async def add_type_title(
    message: Message,
    session: AsyncSession,
    bot: Bot,
    clock: Clock,
    state: FSMContext,
) -> None:
    title = (message.text or "").strip()
    if not title or len(title) > 100:
        await _replace_after_input(
            message,
            session,
            bot,
            clock,
            "Название должно содержать от 1 до 100 символов.",
            reply_markup=back_keyboard("admfaq:types"),
        )
        return
    await faq_repo.create_type(session, title=title)
    await session.commit()
    await state.clear()
    await UiService(clock).remove_incoming(bot, message.chat.id, message.message_id)
    await _render_types(
        session,
        bot,
        clock,
        chat_id=message.chat.id,
        heading="Тип добавлен последним.\n\nТипы FAQ:",
    )


@router.callback_query(F.data.regexp(r"^admfaqtypeorder:\d+$"))
async def edit_type_order_start(
    query: CallbackQuery,
    session: AsyncSession,
    bot: Bot,
    clock: Clock,
    state: FSMContext,
) -> None:
    await state.clear()
    type_id = _one_int(query.data)
    faq_type = await _type_by_id(session, type_id or 0)
    if faq_type is None:
        await query.answer("Тип не найден", show_alert=True)
        return
    await state.update_data(type_id=faq_type.id)
    await state.set_state(AdminFaqFlow.type_order)
    await query.answer()
    await UiService(clock).render(
        session,
        bot,
        chat_id=query.from_user.id,
        parts=[
            f"Текущий порядок типа «{escape(faq_type.title)}»: {faq_type.sort_order}.\n"
            "Введите новое целое число."
        ],
        reply_markup=back_keyboard(f"admfaqtype:{faq_type.id}"),
    )


@router.message(AdminFaqFlow.type_order, F.text)
async def edit_type_order(
    message: Message,
    session: AsyncSession,
    bot: Bot,
    clock: Clock,
    state: FSMContext,
) -> None:
    data = await state.get_data()
    type_id = data.get("type_id")
    try:
        sort_order = int((message.text or "").strip())
    except ValueError:
        await _replace_after_input(
            message,
            session,
            bot,
            clock,
            "Порядок должен быть целым числом.",
            reply_markup=back_keyboard(
                f"admfaqtype:{type_id}" if isinstance(type_id, int) else "admfaq:types"
            ),
        )
        return
    if not isinstance(type_id, int) or not await faq_repo.update_type_order(
        session, type_id=type_id, sort_order=sort_order
    ):
        await state.clear()
        await _replace_after_input(
            message,
            session,
            bot,
            clock,
            "Тип не найден. Вернитесь к списку типов.",
            reply_markup=back_keyboard("admfaq:types"),
        )
        return
    await session.commit()
    await state.clear()
    await UiService(clock).remove_incoming(bot, message.chat.id, message.message_id)
    await _render_types(
        session,
        bot,
        clock,
        chat_id=message.chat.id,
        heading="Порядок изменён.\n\nТипы FAQ:",
    )


@router.callback_query(F.data.regexp(r"^admfaqtype:\d+$"))
async def show_type(
    query: CallbackQuery,
    session: AsyncSession,
    bot: Bot,
    clock: Clock,
    state: FSMContext,
) -> None:
    await state.clear()
    type_id = _one_int(query.data)
    faq_type = await _type_by_id(session, type_id or 0)
    if faq_type is None:
        await query.answer("Тип не найден", show_alert=True)
        return
    await query.answer()
    await UiService(clock).render(
        session,
        bot,
        chat_id=query.from_user.id,
        parts=[f"<b>{escape(faq_type.title)}</b>\nПорядок: {faq_type.sort_order}"],
        reply_markup=faq_type_card_keyboard(faq_type.id),
    )


@router.callback_query(F.data.regexp(r"^admfaqtypedel:\d+$"))
async def delete_type_confirm(
    query: CallbackQuery,
    session: AsyncSession,
    bot: Bot,
    clock: Clock,
    state: FSMContext,
) -> None:
    await state.clear()
    type_id = _one_int(query.data)
    faq_type = await _type_by_id(session, type_id or 0)
    if faq_type is None:
        await query.answer("Тип не найден", show_alert=True)
        return
    await query.answer()
    await UiService(clock).render(
        session,
        bot,
        chat_id=query.from_user.id,
        parts=[f"Удалить тип «{escape(faq_type.title)}»? Его вопросы останутся без типа."],
        reply_markup=with_back(
            "admfaqtypeconfirm", [(faq_type.id, "Удалить")], f"admfaqtype:{faq_type.id}"
        ),
    )


@router.callback_query(F.data.regexp(r"^admfaqtypeconfirm:\d+$"))
async def delete_type(
    query: CallbackQuery,
    session: AsyncSession,
    bot: Bot,
    clock: Clock,
    state: FSMContext,
) -> None:
    await state.clear()
    type_id = _one_int(query.data)
    orphaned = await faq_repo.disable_type(session, type_id or 0)
    if orphaned < 0:
        await query.answer("Тип не найден", show_alert=True)
        return
    await session.commit()
    await query.answer()
    await _render_types(
        session,
        bot,
        clock,
        chat_id=query.from_user.id,
        heading=f"Тип удалён. Без типа: {orphaned}.\n\nТипы FAQ:",
    )


@router.callback_query(F.data == "admfaq:questions")
async def choose_question_type(
    query: CallbackQuery,
    session: AsyncSession,
    bot: Bot,
    clock: Clock,
    state: FSMContext,
) -> None:
    await state.clear()
    await query.answer()
    await _render_question_types(session, bot, clock, chat_id=query.from_user.id)


@router.callback_query(F.data.regexp(r"^admfaqquestions:(?:orphan|\d+)$"))
async def show_questions(
    query: CallbackQuery,
    session: AsyncSession,
    bot: Bot,
    clock: Clock,
    state: FSMContext,
) -> None:
    await state.clear()
    raw = (query.data or "").rsplit(":", 1)[-1]
    type_id = None if raw == "orphan" else int(raw)
    await query.answer()
    await _render_questions(session, bot, clock, chat_id=query.from_user.id, type_id=type_id)


@router.callback_query(F.data.regexp(r"^admfaqadd:\d+$"))
async def add_item_start(
    query: CallbackQuery,
    session: AsyncSession,
    bot: Bot,
    clock: Clock,
    state: FSMContext,
) -> None:
    await state.clear()
    type_id = _one_int(query.data)
    if await _type_by_id(session, type_id or 0) is None:
        await query.answer("Тип не найден", show_alert=True)
        return
    await state.update_data(type_id=type_id)
    await state.set_state(AdminFaqFlow.question)
    await query.answer()
    await UiService(clock).render(
        session,
        bot,
        chat_id=query.from_user.id,
        parts=["Введите вопрос."],
        reply_markup=back_keyboard(f"admfaqquestions:{type_id}"),
    )


@router.message(AdminFaqFlow.question, F.text)
async def add_item_question(
    message: Message,
    session: AsyncSession,
    bot: Bot,
    clock: Clock,
    state: FSMContext,
) -> None:
    question = (message.text or "").strip()
    data = await state.get_data()
    type_id = data.get("type_id")
    if not question or len(question) > 500 or not isinstance(type_id, int):
        await _replace_after_input(
            message,
            session,
            bot,
            clock,
            "Вопрос должен содержать от 1 до 500 символов.",
            reply_markup=back_keyboard(f"admfaqquestions:{type_id}"),
        )
        return
    await state.update_data(question=question)
    await state.set_state(AdminFaqFlow.answer)
    await _replace_after_input(
        message,
        session,
        bot,
        clock,
        "Введите ответ.",
        reply_markup=back_keyboard(f"admfaqquestions:{type_id}"),
    )


@router.message(AdminFaqFlow.answer, F.text)
async def add_item_answer(
    message: Message,
    session: AsyncSession,
    bot: Bot,
    clock: Clock,
    state: FSMContext,
) -> None:
    data = await state.get_data()
    type_id, question = data.get("type_id"), data.get("question")
    answer = (message.text or "").strip()
    if (
        not isinstance(type_id, int)
        or not isinstance(question, str)
        or not answer
        or len(answer) > 4000
    ):
        await state.clear()
        await _replace_after_input(
            message,
            session,
            bot,
            clock,
            "Данные некорректны. Начните заново.",
            reply_markup=back_keyboard("admfaq:questions"),
        )
        return
    await faq_repo.create_item(session, type_id=type_id, question=question, answer=answer)
    await session.commit()
    await state.clear()
    await UiService(clock).remove_incoming(bot, message.chat.id, message.message_id)
    await _render_questions(
        session,
        bot,
        clock,
        chat_id=message.chat.id,
        type_id=type_id,
        heading="Вопрос добавлен.",
    )


@router.callback_query(F.data.regexp(r"^admfaqitem:\d+$"))
async def show_item(
    query: CallbackQuery,
    session: AsyncSession,
    bot: Bot,
    clock: Clock,
    state: FSMContext,
) -> None:
    await state.clear()
    item_id = _one_int(query.data)
    value = await faq_repo.item(session, item_id or 0)
    if value is None:
        await query.answer("Вопрос не найден", show_alert=True)
        return
    await query.answer()
    await UiService(clock).render(
        session,
        bot,
        chat_id=query.from_user.id,
        parts=[f"<b>{escape(value.question)}</b>\n\n{escape(value.answer)}"],
        reply_markup=faq_item_card_keyboard(value.id, type_id=value.type_id),
    )


@router.callback_query(F.data.regexp(r"^admfaqitemmove:\d+$"))
async def move_item(
    query: CallbackQuery,
    session: AsyncSession,
    bot: Bot,
    clock: Clock,
    state: FSMContext,
) -> None:
    await state.clear()
    item_id = _one_int(query.data)
    value = await faq_repo.item(session, item_id or 0)
    if value is None:
        await query.answer("Вопрос не найден", show_alert=True)
        return
    rows = [row for row in await faq_repo.types(session) if row.id != value.type_id]
    if not rows:
        await query.answer("Нет другого активного типа", show_alert=True)
        return
    await query.answer()
    await UiService(clock).render(
        session,
        bot,
        chat_id=query.from_user.id,
        parts=["Выберите новый тип:"],
        reply_markup=faq_item_move_keyboard(value.id, [(row.id, row.title) for row in rows]),
    )


@router.callback_query(F.data.regexp(r"^admfaqitemassign:\d+:\d+$"))
async def assign_item(
    query: CallbackQuery,
    session: AsyncSession,
    bot: Bot,
    clock: Clock,
    state: FSMContext,
) -> None:
    await state.clear()
    try:
        _, item_raw, type_raw = (query.data or "").split(":")
        item_id, type_id = int(item_raw), int(type_raw)
    except ValueError:
        await query.answer("Некорректные данные", show_alert=True)
        return
    if not await faq_repo.assign_item(session, item_id=item_id, type_id=type_id):
        await query.answer("Вопрос или тип не найден", show_alert=True)
        return
    await session.commit()
    await query.answer()
    await _render_questions(
        session,
        bot,
        clock,
        chat_id=query.from_user.id,
        type_id=type_id,
        heading="Вопрос перенесён.",
    )


@router.callback_query(F.data.regexp(r"^admfaqitemdel:\d+$"))
async def delete_item_confirm(
    query: CallbackQuery,
    session: AsyncSession,
    bot: Bot,
    clock: Clock,
    state: FSMContext,
) -> None:
    await state.clear()
    item_id = _one_int(query.data)
    value = await faq_repo.item(session, item_id or 0)
    if value is None:
        await query.answer("Вопрос не найден", show_alert=True)
        return
    await query.answer()
    await UiService(clock).render(
        session,
        bot,
        chat_id=query.from_user.id,
        parts=[f"Удалить вопрос «{escape(value.question)}»?"],
        reply_markup=with_back(
            "admfaqitemconfirm", [(value.id, "Удалить")], f"admfaqitem:{value.id}"
        ),
    )


@router.callback_query(F.data.regexp(r"^admfaqitemconfirm:\d+$"))
async def delete_item(
    query: CallbackQuery,
    session: AsyncSession,
    bot: Bot,
    clock: Clock,
    state: FSMContext,
) -> None:
    await state.clear()
    item_id = _one_int(query.data)
    value: FaqItem | None = await faq_repo.item(session, item_id or 0)
    if value is None:
        await query.answer("Вопрос не найден", show_alert=True)
        return
    parent_type_id = value.type_id
    if not await faq_repo.disable_item(session, value.id):
        await query.answer("Вопрос не найден", show_alert=True)
        return
    await session.commit()
    await query.answer()
    await _render_questions(
        session,
        bot,
        clock,
        chat_id=query.from_user.id,
        type_id=parent_type_id,
        heading="Вопрос удалён.",
    )


@router.callback_query(F.data == "admfaq:download")
async def download_excel(
    query: CallbackQuery,
    session: AsyncSession,
    bot: Bot,
    clock: Clock,
) -> None:
    type_rows = await faq_repo.types(session)
    question_rows = await faq_repo.all_active_items(session)
    next_id = await faq_repo.next_type_id(session)
    payload = build_faq_workbook(
        types=type_rows,
        questions=question_rows,
        next_type_id=next_id,
    )
    await bot.send_document(
        query.from_user.id,
        BufferedInputFile(payload, filename="faq-template.xlsx"),
    )
    await query.answer("Шаблон отправлен")
    await UiService(clock).render(
        session,
        bot,
        chat_id=query.from_user.id,
        parts=["Вопросы и ответы:"],
        reply_markup=faq_admin_menu(),
    )


@router.callback_query(F.data == "admfaq:upload")
async def upload_excel_start(
    query: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
    clock: Clock,
) -> None:
    await state.clear()
    await state.set_state(AdminFaqFlow.excel_upload)
    await query.answer()
    await UiService(clock).render(
        session,
        bot,
        chat_id=query.from_user.id,
        parts=["Пришлите заполненный XLSX-файл. Перед каждым импортом скачивайте свежий шаблон."],
        reply_markup=faq_upload_keyboard(),
    )


@router.message(AdminFaqFlow.excel_upload, F.document)
async def upload_excel(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
    clock: Clock,
) -> None:
    document = message.document
    if document is None:
        return
    filename = document.file_name or ""
    allowed_mime_types = {
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/octet-stream",
    }
    if not filename.lower().endswith(".xlsx") or document.mime_type not in allowed_mime_types:
        await _replace_after_input(
            message,
            session,
            bot,
            clock,
            "Нужен файл XLSX, скачанный из этого раздела.",
            reply_markup=faq_upload_keyboard(),
        )
        return
    if document.file_size is not None and document.file_size > MAX_FILE_SIZE:
        await _replace_after_input(
            message,
            session,
            bot,
            clock,
            "Файл не должен превышать 5 МБ.",
            reply_markup=faq_upload_keyboard(),
        )
        return

    from io import BytesIO

    buffer = BytesIO()
    await bot.download(document, destination=buffer)
    payload = buffer.getvalue()
    try:
        data = parse_faq_workbook(payload)
    except FaqWorkbookError as exc:
        await _replace_after_input(
            message,
            session,
            bot,
            clock,
            f"Файл не загружен.\n{escape(str(exc))}",
            reply_markup=faq_upload_keyboard(),
        )
        return
    except Exception as exc:
        logger.opt(exception=exc).error(
            "Не удалось разобрать XLSX FAQ: {}: {}", type(exc).__name__, exc
        )
        await _replace_after_input(
            message,
            session,
            bot,
            clock,
            "Файл не загружен: не удалось прочитать структуру XLSX. "
            "Скачайте свежий шаблон и перенесите данные в него.",
            reply_markup=faq_upload_keyboard(),
        )
        return

    try:
        # IsAdmin может уже открыть неявную транзакцию чтением сотрудника, а
        # bootstrap-администратор проходит фильтр без запроса. Поддерживаем
        # оба пути, сохраняя весь импорт в одной транзакции.
        if session.in_transaction():
            summary = await apply_faq_workbook(session, data)
            await session.commit()
        else:
            async with session.begin():
                summary = await apply_faq_workbook(session, data)
    except FaqWorkbookError as exc:
        if session.in_transaction():
            await session.rollback()
        await _replace_after_input(
            message,
            session,
            bot,
            clock,
            f"Файл не загружен.\n{escape(str(exc))}",
            reply_markup=faq_upload_keyboard(),
        )
        return

    await state.clear()
    await UiService(clock).remove_incoming(bot, message.chat.id, message.message_id)
    await UiService(clock).render(
        session,
        bot,
        chat_id=message.chat.id,
        parts=[format_import_summary(summary)],
        reply_markup=faq_admin_menu(),
    )


@router.message(AdminFaqFlow.excel_upload)
async def upload_excel_requires_document(
    message: Message,
    session: AsyncSession,
    bot: Bot,
    clock: Clock,
) -> None:
    await _replace_after_input(
        message,
        session,
        bot,
        clock,
        "Пришлите XLSX-файл как документ.",
        reply_markup=faq_upload_keyboard(),
    )
