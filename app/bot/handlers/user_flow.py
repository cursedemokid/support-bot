"""Личный кабинет клиента: инструкция, поиск товара и обращение в поддержку."""

from __future__ import annotations

import datetime as dt

from aiogram import Bot, F, Router, html
from aiogram.filters import BaseFilter, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import callbacks
from app.bot.filters.access import IsClient
from app.bot.keyboards import (
    categories_keyboard,
    chat_keyboard,
    client_menu,
    instructions_menu,
    manager_notification_keyboard,
    product_keyboard,
    support_menu,
    ticket_keyboard,
    with_back,
)
from app.catalog import product_card_parts
from app.clock import Clock
from app.config import AppConfig
from app.db.models import MessageHistory
from app.media import resolve_media
from app.repositories import content as content_repo
from app.repositories import faq as faq_repo
from app.repositories import messages as messages_repo
from app.repositories import staff as staff_repo
from app.repositories import tickets as tickets_repo
from app.services.tickets import TicketService
from app.services.ui import UiPhoto, UiService

router = Router(name="user_flow")
router.message.filter(IsClient())
router.callback_query.filter(IsClient())


class ClientFlow(StatesGroup):
    waiting_product_query = State()
    waiting_category = State()
    waiting_description = State()
    writing_ticket = State()
    choosing_ticket = State()


# Совместимое имя для внешних импортов; сценарий теперь начинается с меню.
TicketFlow = ClientFlow


def _ui(clock: Clock) -> UiService:
    return UiService(clock)


def _short(text: str, limit: int = 52) -> str:
    compact = " ".join(text.split())
    return compact if len(compact) <= limit else f"{compact[: limit - 1]}…"


def _history_parts(
    history: list[MessageHistory], viewer_id: int, clock: Clock
) -> list[str | UiPhoto]:
    """Визуальный чат: даты и время, но не UUID и не технический ID."""
    parts: list[str | UiPhoto] = []
    day = None
    for item in history:
        local = item.sent_at.astimezone(clock.tz)
        if local.date() != day:
            day = local.date()
            parts.append(f"<b>──────── {day:%d.%m.%Y} ────────</b>")
        author = "Вы" if item.from_user_id == viewer_id else "Менеджер"
        header = f"<b>{author} · {local:%H:%M}</b>"
        if item.photo_file_id is not None:
            parts.append(UiPhoto(item.photo_file_id, f"{header}\n{html.quote(item.message_text)}"))
        else:
            parts.append(f"{header}\n{html.quote(item.message_text)}")
    if not parts:
        return ["В этом обращении пока нет сообщений."]
    output: list[str | UiPhoto] = []
    text_parts: list[str] = []
    for part in parts:
        if isinstance(part, UiPhoto):
            if text_parts:
                output.append("\n\n".join(text_parts))
                text_parts = []
            output.append(part)
        else:
            text_parts.append(part)
    if text_parts:
        output.append("\n\n".join(text_parts))
    return output


async def _render_client_menu(session: AsyncSession, bot: Bot, chat_id: int, clock: Clock) -> None:
    await _ui(clock).render(
        session,
        bot,
        chat_id=chat_id,
        parts=["Здравствуйте! Выберите, что хотите сделать."],
        reply_markup=client_menu(),
    )


async def _render_pending_ticket_choice(
    session: AsyncSession,
    bot: Bot,
    *,
    chat_id: int,
    state: FSMContext,
    clock: Clock,
    prompt: str = "К какому обращению относится сохранённое сообщение?",
) -> bool:
    if not isinstance((await state.get_data()).get("pending_client_message"), dict):
        return False
    tickets = await tickets_repo.list_open_for_user(session, chat_id)
    rows: list[tuple[int | str, str]] = [
        (item.id, f"№{item.id} · {_short(item.title or item.description)}") for item in tickets
    ]
    markup = with_back("clientticket", rows, "clientpending:cancel", "Отменить отправку")
    parts = [prompt]
    if not tickets:
        parts = ["Открытых обращений больше нет. Сообщение сохранено до отмены отправки."]
    await state.set_state(ClientFlow.choosing_ticket)
    await _ui(clock).render(
        session,
        bot,
        chat_id=chat_id,
        parts=parts,
        reply_markup=markup,
    )
    return True


async def _render_client_tickets(
    session: AsyncSession, bot: Bot, chat_id: int, clock: Clock, *, pending: bool = False
) -> None:
    tickets = await tickets_repo.list_for_user(session, chat_id)
    if not tickets:
        await _ui(clock).render(
            session,
            bot,
            chat_id=chat_id,
            parts=["У вас пока нет обращений."],
            reply_markup=(
                with_back("tkt", [], "clientpending:cancel", "Отменить отправку")
                if pending
                else chat_keyboard(writable=False, back_data="nav:support")
            ),
        )
        return
    await _ui(clock).render(
        session,
        bot,
        chat_id=chat_id,
        parts=[
            "<b>Мои обращения</b>\n\n"
            + ("Сообщение сохранено. Выберите чат или отмените отправку." if pending else "")
        ],
        reply_markup=with_back(
            "tkt",
            [(item.id, _short(item.description)) for item in tickets],
            "clientpending:cancel" if pending else "nav:support",
            "Отменить отправку" if pending else "⬅️ Назад",
        ),
    )


class _PendingMessageNavigation(BaseFilter):
    async def __call__(self, query: CallbackQuery, state: FSMContext) -> bool:
        data = await state.get_data()
        callback = query.data or ""
        return isinstance(data.get("pending_client_message"), dict) and not (
            callback.startswith("clientticket:")
            or callback == "clientpending:cancel"
            or callback == "sup:tickets"
        )


async def _render_support_menu(session: AsyncSession, bot: Bot, chat_id: int, clock: Clock) -> None:
    await _ui(clock).render(
        session,
        bot,
        chat_id=chat_id,
        parts=["<b>Техническая поддержка</b>"],
        reply_markup=support_menu(),
    )


async def _render_ticket(
    session: AsyncSession,
    bot: Bot,
    *,
    ticket_id: int,
    viewer_id: int,
    clock: Clock,
    manager: bool = False,
) -> bool:
    ticket = await tickets_repo.get_ticket(session, ticket_id)
    if ticket is None or (not manager and ticket.telegram_user_id != viewer_id):
        return False
    history = await messages_repo.get_history(session, ticket.id)
    writable = ticket.state != "closed" and (not manager or ticket.manager_id in (None, viewer_id))
    take_id = ticket.id if manager and ticket.state == "new" else None
    await _ui(clock).render(
        session,
        bot,
        chat_id=viewer_id,
        parts=_history_parts(history, viewer_id, clock),
        reply_markup=chat_keyboard(
            writable=writable,
            take_ticket_id=take_id,
            back_data="mgr:list" if manager else "sup:tickets",
        ),
    )
    return True


async def _menu(message: Message, service: TicketService) -> None:
    author = message.from_user
    if author is not None and (service.is_manager(author.id) or service.is_admin(author.id)):
        from app.bot.keyboards import manager_menu

        await message.answer(
            "Личный кабинет сотрудника.",
            reply_markup=manager_menu(admin=service.is_admin(author.id)),
        )
    else:
        await message.answer(
            "Здравствуйте! Выберите, что хотите сделать.", reply_markup=client_menu()
        )


@router.message(CommandStart())
async def handle_start(
    message: Message,
    state: FSMContext,
    config: AppConfig,
    service: TicketService,
    session: AsyncSession | None = None,
    bot: Bot | None = None,
    clock: Clock | None = None,
) -> None:
    if isinstance((await state.get_data()).get("pending_client_message"), dict):
        if session is not None and bot is not None and clock is not None:
            await _ui(clock).remove_incoming(bot, message.chat.id, message.message_id)
            await _render_pending_ticket_choice(
                session,
                bot,
                chat_id=message.chat.id,
                state=state,
                clock=clock,
                prompt="Сначала выберите обращение или отмените отправку сохранённого сообщения.",
            )
        else:
            await message.answer("Сначала выберите обращение для сохранённого сообщения.")
        return
    await state.clear()
    if (
        message.from_user is not None
        and message.from_user.username is not None
        and session is not None
    ):
        invited = await staff_repo.activate_invitation(
            session,
            telegram_user_id=message.from_user.id,
            username=message.from_user.username,
            now=clock.now() if clock is not None else dt.datetime.now(tz=dt.UTC),
        )
        if invited is not None:
            await session.commit()
            if bot is not None and clock is not None:
                from app.bot.keyboards import manager_menu

                await _ui(clock).remove_incoming(bot, message.chat.id, message.message_id)
                await _ui(clock).render(
                    session,
                    bot,
                    chat_id=message.from_user.id,
                    parts=["Приглашение принято. Личный кабинет сотрудника готов."],
                    reply_markup=manager_menu(admin=invited.role == "admin"),
                )
                return
    if (
        message.from_user is not None
        and session is not None
        and await service.is_staff(session, message.from_user.id)
    ):
        if bot is not None and clock is not None:
            from app.bot.keyboards import manager_menu

            await _ui(clock).remove_incoming(bot, message.chat.id, message.message_id)
            await _ui(clock).render(
                session,
                bot,
                chat_id=message.from_user.id,
                parts=["Личный кабинет сотрудника."],
                reply_markup=manager_menu(
                    admin=await service.is_database_admin(session, message.from_user.id)
                ),
            )
        else:
            await message.answer("Личный кабинет сотрудника.")
        return
    # QR со старым ключом категории остаётся рабочим: упаковка уже могла уйти
    # в печать, но обычный /start всегда открывает новое клиентское меню.
    payload = (message.text or "").split(maxsplit=1)
    if (
        len(payload) == 2
        and message.from_user is not None
        and not service.is_manager(message.from_user.id)
        and not service.is_admin(message.from_user.id)
    ):
        key = payload[1].strip()
        db_category = await content_repo.category(session, key) if session is not None else None
        valid_categories = {item.key for item in config.settings.support.categories}
        if db_category is not None or (session is None and key in valid_categories):
            await state.set_state(ClientFlow.waiting_description)
            await state.update_data(category=key)
            if session is not None and bot is not None and clock is not None:
                await _ui(clock).remove_incoming(bot, message.chat.id, message.message_id)
                await _ui(clock).render(
                    session,
                    bot,
                    chat_id=message.from_user.id,
                    parts=["Опишите проблему или прикрепите фото."],
                    reply_markup=chat_keyboard(writable=False, back_data="nav:support"),
                )
            else:
                await message.answer("Опишите проблему или прикрепите фото.")
            return
    if session is not None and bot is not None and clock is not None:
        await _ui(clock).remove_incoming(bot, message.chat.id, message.message_id)
        if message.from_user is not None and (
            service.is_manager(message.from_user.id) or service.is_admin(message.from_user.id)
        ):
            from app.bot.keyboards import manager_menu

            await _ui(clock).render(
                session,
                bot,
                chat_id=message.from_user.id,
                parts=["Личный кабинет сотрудника."],
                reply_markup=manager_menu(admin=service.is_admin(message.from_user.id)),
            )
        else:
            await _render_client_menu(session, bot, message.chat.id, clock)
        return
    await _menu(message, service)


@router.callback_query(_PendingMessageNavigation())
async def keep_pending_message_during_navigation(
    query: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
    service: TicketService,
    clock: Clock,
) -> None:
    if await service.is_staff(session, query.from_user.id):
        await query.answer("Сотрудникам недоступен клиентский чат", show_alert=True)
        return
    await query.answer("Сначала выберите чат или отмените отправку.", show_alert=True)
    await _render_pending_ticket_choice(
        session,
        bot,
        chat_id=query.from_user.id,
        state=state,
        clock=clock,
    )


@router.callback_query(F.data == "clientpending:cancel")
async def cancel_pending_client_message(
    query: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
    service: TicketService,
    clock: Clock,
) -> None:
    if await service.is_staff(session, query.from_user.id):
        await query.answer("Сотрудникам недоступен клиентский чат", show_alert=True)
        return
    if not isinstance((await state.get_data()).get("pending_client_message"), dict):
        await query.answer("Отправка уже отменена", show_alert=True)
        return
    await state.clear()
    await query.answer("Отправка отменена")
    await _render_client_tickets(session, bot, query.from_user.id, clock)


@router.callback_query(F.data == "nav:catalog")
async def show_catalog_home(
    query: CallbackQuery, session: AsyncSession, bot: Bot, clock: Clock
) -> None:
    await query.answer()
    products = await content_repo.find_products(session, "")
    await _ui(clock).render(
        session,
        bot,
        chat_id=query.from_user.id,
        parts=[
            "<b>Инструкции к товарам</b>\n\n"
            "Выберите товар из списка или найдите его по артикулу или названию."
        ],
        reply_markup=instructions_menu(products),
    )


@router.callback_query(F.data == "nav:support")
async def show_support_home(
    query: CallbackQuery, state: FSMContext, session: AsyncSession, bot: Bot, clock: Clock
) -> None:
    await state.clear()
    await query.answer()
    await _render_support_menu(session, bot, query.from_user.id, clock)


@router.callback_query(F.data == "menu:root")
async def show_root(
    query: CallbackQuery, state: FSMContext, session: AsyncSession, bot: Bot, clock: Clock
) -> None:
    await state.clear()
    await query.answer()
    await _render_client_menu(session, bot, query.from_user.id, clock)


@router.callback_query(F.data == "nav:product-search")
async def start_product_search(
    query: CallbackQuery, state: FSMContext, session: AsyncSession, bot: Bot, clock: Clock
) -> None:
    await state.set_state(ClientFlow.waiting_product_query)
    await query.answer()
    await _ui(clock).render(
        session,
        bot,
        chat_id=query.from_user.id,
        parts=["Введите артикул или название товара."],
        reply_markup=chat_keyboard(writable=False, back_data="nav:catalog"),
    )


@router.callback_query(F.data == "sup:create")
async def start_ticket(
    query: CallbackQuery, state: FSMContext, session: AsyncSession, bot: Bot, clock: Clock
) -> None:
    rows = await content_repo.categories(session)
    if not rows:
        await query.answer("Категории пока не настроены", show_alert=True)
        return
    await state.set_state(ClientFlow.waiting_category)
    await query.answer()
    await _ui(clock).render(
        session,
        bot,
        chat_id=query.from_user.id,
        parts=["Выберите, с чем нужна помощь:"],
        reply_markup=categories_keyboard(rows, back_data="nav:support"),
    )


@router.callback_query(F.data == "sup:tickets")
async def client_ticket_list(
    query: CallbackQuery, state: FSMContext, session: AsyncSession, bot: Bot, clock: Clock
) -> None:
    if isinstance((await state.get_data()).get("pending_client_message"), dict):
        await query.answer("Сообщение сохранено; выберите чат или отмените отправку.")
        await _render_client_tickets(session, bot, query.from_user.id, clock, pending=True)
        return
    await state.clear()
    await query.answer()
    await _render_client_tickets(session, bot, query.from_user.id, clock)


@router.callback_query(F.data.startswith("tkt:"))
async def open_client_ticket(
    query: CallbackQuery, state: FSMContext, session: AsyncSession, bot: Bot, clock: Clock
) -> None:
    try:
        ticket_id = int((query.data or "").split(":", 1)[1])
    except ValueError:
        await query.answer("Обращение не найдено", show_alert=True)
        return
    await state.clear()
    if not await _render_ticket(
        session, bot, ticket_id=ticket_id, viewer_id=query.from_user.id, clock=clock
    ):
        await query.answer("Это обращение недоступно", show_alert=True)
        return
    await state.update_data(ticket_id=ticket_id)
    await query.answer()


@router.callback_query(F.data == "chat:write")
async def begin_client_writing(
    query: CallbackQuery, state: FSMContext, session: AsyncSession, bot: Bot, clock: Clock
) -> None:
    data = await state.get_data()
    ticket_id = data.get("ticket_id")
    # `ticket_id` is set below for a just-opened chat; re-read it from the
    # previous callback only, never from callback data supplied by a client.
    if not isinstance(ticket_id, int):
        await query.answer("Сначала откройте обращение", show_alert=True)
        return
    ticket = await tickets_repo.get_ticket(session, ticket_id)
    if ticket is None or ticket.telegram_user_id != query.from_user.id or ticket.state == "closed":
        await query.answer("В это обращение уже нельзя написать", show_alert=True)
        return
    history = await messages_repo.get_history(session, ticket.id)
    await state.set_state(ClientFlow.writing_ticket)
    await query.answer()
    await _ui(clock).render(
        session,
        bot,
        chat_id=query.from_user.id,
        parts=[
            *_history_parts(history, query.from_user.id, clock),
            "<i>Напишите сообщение. Оно будет добавлено в это обращение.</i>",
        ],
        reply_markup=chat_keyboard(writable=False, back_data="chat:read"),
    )


@router.callback_query(F.data == "chat:read")
async def return_to_client_reading(
    query: CallbackQuery, state: FSMContext, session: AsyncSession, bot: Bot, clock: Clock
) -> None:
    ticket_id = (await state.get_data()).get("ticket_id")
    await state.clear()
    if not isinstance(ticket_id, int) or not await _render_ticket(
        session, bot, ticket_id=ticket_id, viewer_id=query.from_user.id, clock=clock
    ):
        await query.answer("Обращение не найдено", show_alert=True)
        return
    await state.update_data(ticket_id=ticket_id)
    await query.answer()


@router.callback_query(F.data == "sup:faq")
async def faq_types(query: CallbackQuery, session: AsyncSession, bot: Bot, clock: Clock) -> None:
    rows = await faq_repo.types(session)
    await query.answer()
    await _ui(clock).render(
        session,
        bot,
        chat_id=query.from_user.id,
        parts=["<b>Вопросы и ответы</b>"],
        reply_markup=with_back("faqtype", [(x.id, x.title) for x in rows], "nav:support"),
    )


@router.callback_query(F.data.startswith("faqtype:"))
async def faq_questions(
    query: CallbackQuery, session: AsyncSession, bot: Bot, clock: Clock
) -> None:
    try:
        type_id = int((query.data or "").split(":", 1)[1])
    except ValueError:
        await query.answer("Раздел не найден", show_alert=True)
        return
    rows = await faq_repo.items(session, type_id)
    await query.answer()
    await _ui(clock).render(
        session,
        bot,
        chat_id=query.from_user.id,
        parts=["Выберите вопрос:"],
        reply_markup=with_back("faq", [(x.id, x.question) for x in rows], "sup:faq"),
    )


@router.callback_query(F.data.startswith("faq:"))
async def faq_answer(query: CallbackQuery, session: AsyncSession, bot: Bot, clock: Clock) -> None:
    try:
        item_id = int((query.data or "").split(":", 1)[1])
    except ValueError:
        item_id = 0
    item = await faq_repo.visible_item(session, item_id)
    if item is None:
        await query.answer("Ответ не найден", show_alert=True)
        return
    await query.answer()
    await _ui(clock).render(
        session,
        bot,
        chat_id=query.from_user.id,
        parts=[f"<b>{html.quote(item.question)}</b>\n\n{html.quote(item.answer)}"],
        reply_markup=chat_keyboard(writable=False, back_data=f"faqtype:{item.type_id}"),
    )


@router.callback_query(F.data == "menu:instructions")
async def show_instructions(
    query: CallbackQuery, session: AsyncSession, bot: Bot, clock: Clock
) -> None:
    rows = await content_repo.sections(session)
    if not rows:
        await query.answer("Инструкции пока добавляются", show_alert=True)
        return
    await query.answer()
    await _ui(clock).render(
        session,
        bot,
        chat_id=query.from_user.id,
        parts=["Выберите раздел инструкции:"],
        reply_markup=with_back("ins", [(x.id, x.title) for x in rows], "menu:root"),
    )


@router.callback_query(F.data.startswith("ins:"))
async def show_instruction(
    query: CallbackQuery, session: AsyncSession, bot: Bot, clock: Clock
) -> None:
    try:
        item = await content_repo.section(session, int((query.data or "").split(":", 1)[1]))
    except ValueError:
        item = None
    if item is None:
        await query.answer("Раздел не найден", show_alert=True)
        return
    await query.answer()
    await _ui(clock).render(
        session,
        bot,
        chat_id=query.from_user.id,
        parts=[f"<b>{html.quote(item.title)}</b>\n\n{html.quote(item.body)}"],
        reply_markup=chat_keyboard(writable=False, back_data="menu:instructions"),
    )


@router.callback_query(F.data == "menu:products")
async def begin_product_search(
    query: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
    clock: Clock,
) -> None:
    await state.set_state(ClientFlow.waiting_product_query)
    await query.answer()
    await _ui(clock).render(
        session,
        bot,
        chat_id=query.from_user.id,
        parts=["Введите артикул или название товара."],
        reply_markup=chat_keyboard(writable=False, back_data="nav:catalog"),
    )


@router.message(ClientFlow.waiting_product_query, F.text)
async def search_product(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
    clock: Clock,
) -> None:
    query = (message.text or "").strip()
    if not query:
        await _ui(clock).render(
            session,
            bot,
            chat_id=message.chat.id,
            parts=["Введите артикул или название товара текстом."],
            reply_markup=chat_keyboard(writable=False, back_data="nav:catalog"),
        )
        return
    await state.clear()
    found = await content_repo.find_products(session, query)
    if not found:
        await _ui(clock).remove_incoming(bot, message.chat.id, message.message_id)
        await _ui(clock).render(
            session,
            bot,
            chat_id=message.chat.id,
            parts=["Товар не найден. Проверьте артикул или напишите в поддержку."],
            reply_markup=chat_keyboard(writable=False, back_data="nav:catalog"),
        )
        return
    await _ui(clock).remove_incoming(bot, message.chat.id, message.message_id)
    await _ui(clock).render(
        session,
        bot,
        chat_id=message.chat.id,
        parts=["Выберите товар:"],
        reply_markup=with_back(
            "prod", [(x.id, f"{x.sku} · {x.title}") for x in found], "nav:catalog"
        ),
    )


@router.callback_query(F.data.startswith("prod:"))
async def show_product(query: CallbackQuery, session: AsyncSession, bot: Bot, clock: Clock) -> None:
    try:
        item = await content_repo.product(session, int((query.data or "").split(":", 1)[1]))
    except ValueError:
        item = None
    if item is None:
        await query.answer("Товар не найден", show_alert=True)
        return
    images = await content_repo.product_images(session, item.id)
    await query.answer()
    await _ui(clock).render(
        session,
        bot,
        chat_id=query.from_user.id,
        parts=product_card_parts(item, [image.file_id for image in images]),
        reply_markup=product_keyboard(item),
    )


@router.callback_query(F.data.startswith("pdf:"))
async def send_product_pdf(query: CallbackQuery, session: AsyncSession, bot: Bot) -> None:
    try:
        item = await content_repo.product(session, int((query.data or "").split(":", 1)[1]))
    except ValueError:
        item = None
    if item is None:
        await query.answer("Инструкция не найдена", show_alert=True)
        return
    await bot.send_document(
        query.from_user.id,
        resolve_media(item.pdf_file_id),
        caption=f"Инструкция: {html.quote(item.title)}",
    )
    await query.answer("Инструкция отправлена")


@router.callback_query(F.data == "menu:support")
async def begin_support(
    query: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
    clock: Clock,
) -> None:
    rows = await content_repo.categories(session)
    if not rows:
        await query.answer("Категории пока не настроены", show_alert=True)
        return
    await state.set_state(ClientFlow.waiting_category)
    await query.answer()
    await _ui(clock).render(
        session,
        bot,
        chat_id=query.from_user.id,
        parts=["Выберите, с чем нужна помощь:"],
        reply_markup=categories_keyboard(rows, back_data="menu:root"),
    )


@router.callback_query(callbacks.Callback(callbacks.PickCategory))
async def choose_category(
    query: CallbackQuery,
    callback_data: callbacks.PickCategory,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
    clock: Clock,
) -> None:
    if await content_repo.category(session, callback_data.key) is None:
        await query.answer("Эта категория больше недоступна", show_alert=True)
        return
    await state.set_state(ClientFlow.waiting_description)
    await state.update_data(category=callback_data.key)
    await query.answer()
    await _ui(clock).render(
        session,
        bot,
        chat_id=query.from_user.id,
        parts=["Опишите проблему или прикрепите фото."],
        reply_markup=chat_keyboard(writable=False, back_data="nav:support"),
    )


async def _save_description(
    message: Message,
    state: FSMContext,
    config: AppConfig,
    service: TicketService,
    session: AsyncSession,
    bot: Bot,
    clock: Clock | None = None,
) -> None:
    text = (message.text or message.caption or "").strip()
    photo = message.photo[-1].file_id if message.photo else None
    category = (await state.get_data()).get("category")
    if (not text and photo is None) or not isinstance(category, str):
        await message.answer("Опишите проблему или прикрепите фото.")
        return
    if len(text) > config.settings.support.max_message_chars:
        await message.answer("Сообщение слишком длинное. Сократите, пожалуйста.")
        return
    await state.clear()
    author = message.from_user
    if author is None:
        return
    created = await service.create_ticket(
        session,
        bot,
        telegram_user_id=author.id,
        telegram_username=author.username,
        category_key=category,
        description=text or "📷 Фото",
        photo_file_id=photo,
        card_markup=lambda ticket_id: ticket_keyboard(ticket_id, taken=False),
    )
    await session.commit()
    if clock is not None:
        await _ui(clock).remove_incoming(bot, message.chat.id, message.message_id)
        await _ui(clock).render(
            session,
            bot,
            chat_id=author.id,
            parts=[
                f"Обращение <b>№{created.ticket.id}</b> принято. "
                "Если сотруднику понадобится дополнительная информация, он напишет сюда."
            ],
            reply_markup=support_menu(),
        )
        return
    await message.answer(
        f"Обращение <b>№{created.ticket.id}</b> принято. "
        "Если менеджеру будет нужна дополнительная информация, он напишет сюда."
    )


@router.message(ClientFlow.waiting_description, F.text)
async def handle_description(
    message: Message,
    state: FSMContext,
    config: AppConfig,
    service: TicketService,
    session: AsyncSession,
    bot: Bot,
    clock: Clock | None = None,
) -> None:
    await _save_description(message, state, config, service, session, bot, clock)


@router.message(ClientFlow.waiting_description, F.photo)
async def handle_description_photo(
    message: Message,
    state: FSMContext,
    config: AppConfig,
    service: TicketService,
    session: AsyncSession,
    bot: Bot,
    clock: Clock,
) -> None:
    await _save_description(message, state, config, service, session, bot, clock)


@router.message(ClientFlow.writing_ticket, F.text)
async def append_to_selected_ticket(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
    service: TicketService,
    clock: Clock,
) -> None:
    await _append_to_selected_ticket(message, state, session, bot, service, clock)


@router.message(ClientFlow.writing_ticket, F.photo)
async def append_photo_to_selected_ticket(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
    service: TicketService,
    clock: Clock,
) -> None:
    await _append_to_selected_ticket(message, state, session, bot, service, clock)


async def _append_to_selected_ticket(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
    service: TicketService,
    clock: Clock,
) -> None:
    text = (message.text or message.caption or "").strip()
    photo = message.photo[-1].file_id if message.photo else None
    author = message.from_user
    ticket_id = (await state.get_data()).get("ticket_id")
    if author is None or (not text and photo is None) or not isinstance(ticket_id, int):
        return
    ticket = await tickets_repo.get_ticket(session, ticket_id)
    if ticket is None or ticket.telegram_user_id != author.id or ticket.state == "closed":
        await state.clear()
        await _render_support_menu(session, bot, author.id, clock)
        return
    result = await service.relay_from_client(
        session,
        bot,
        ticket_id=ticket_id,
        text=text or "📷 Фото",
        photo_file_id=photo,
        card_markup=manager_notification_keyboard,
    )
    await session.commit()
    await _ui(clock).remove_incoming(bot, message.chat.id, message.message_id)
    await state.clear()
    if not result.ok:
        await _ui(clock).render(
            session,
            bot,
            chat_id=author.id,
            parts=["Сообщение сохранено, но менеджерам пока не удалось его доставить."],
            reply_markup=chat_keyboard(writable=False, back_data="sup:tickets"),
        )
        return
    await _render_ticket(session, bot, ticket_id=ticket_id, viewer_id=author.id, clock=clock)
    await state.update_data(ticket_id=ticket_id)


async def _route_unprompted_message(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
    service: TicketService,
    clock: Clock,
) -> None:
    author = message.from_user
    if author is None or await service.is_staff(session, author.id):
        return
    pending = (await state.get_data()).get("pending_client_message")
    if isinstance(pending, dict):
        await _render_pending_ticket_choice(
            session,
            bot,
            chat_id=author.id,
            state=state,
            clock=clock,
            prompt=(
                "Предыдущее сообщение сохранено. Новое сообщение пока не отправлено; "
                "после выбора чата отправьте его повторно."
            ),
        )
        return
    text = (message.text or message.caption or "").strip()
    photo = message.photo[-1].file_id if message.photo else None
    if not text and photo is None:
        await message.answer("Отправьте текст или фото по обращению. Начните с /start.")
        return

    ticket = await service.ticket_for_reply(session, telegram_user_id=author.id)
    if ticket is not None:
        await _relay_unprompted_message(
            message, ticket.id, text, photo, session, bot, service, clock, state
        )
        return

    open_tickets = await tickets_repo.list_open_for_user(session, author.id)
    if len(open_tickets) > 1:
        await state.update_data(
            pending_client_message={
                "text": text,
                "photo_file_id": photo,
                "source_chat_id": message.chat.id,
                "source_message_id": message.message_id,
            }
        )
        await state.set_state(ClientFlow.choosing_ticket)
        await _ui(clock).render(
            session,
            bot,
            chat_id=author.id,
            parts=["К какому обращению относится сообщение?"],
            reply_markup=with_back(
                "clientticket",
                [
                    (item.id, f"№{item.id} · {_short(item.title or item.description)}")
                    for item in open_tickets
                ],
                "clientpending:cancel",
                "Отменить отправку",
            ),
        )
        return

    await message.answer("У вас нет открытого обращения. Начните с /start, чтобы создать его.")


async def _relay_unprompted_message(
    message: Message,
    ticket_id: int,
    text: str,
    photo: str | None,
    session: AsyncSession,
    bot: Bot,
    service: TicketService,
    clock: Clock,
    state: FSMContext,
) -> None:
    ticket = await tickets_repo.get_ticket(session, ticket_id)
    author = message.from_user
    if (
        ticket is None
        or author is None
        or ticket.telegram_user_id != author.id
        or ticket.state == "closed"
    ):
        await _ui(clock).render(
            session,
            bot,
            chat_id=message.chat.id,
            parts=["Это обращение уже закрыто или недоступно. Начните с /start."],
            reply_markup=chat_keyboard(writable=False, back_data="sup:tickets"),
        )
        return
    result = await service.relay_from_client(
        session,
        bot,
        ticket_id=ticket_id,
        text=text or "📷 Фото",
        photo_file_id=photo,
        card_markup=manager_notification_keyboard,
    )
    await session.commit()
    if result.ok:
        await _ui(clock).remove_incoming(bot, message.chat.id, message.message_id)
        await _render_ticket(session, bot, ticket_id=ticket_id, viewer_id=author.id, clock=clock)
        await state.update_data(ticket_id=ticket_id)
    else:
        await _ui(clock).render(
            session,
            bot,
            chat_id=author.id,
            parts=["Сообщение сохранено, но менеджерам пока не удалось его доставить."],
            reply_markup=chat_keyboard(writable=False, back_data="sup:tickets"),
        )


@router.callback_query(F.data.startswith("clientticket:"))
async def select_ticket_for_pending_message(
    query: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
    service: TicketService,
    clock: Clock,
) -> None:
    author = query.from_user
    try:
        ticket_id = int((query.data or "").split(":", 1)[1])
    except ValueError:
        await query.answer("Обращение не найдено", show_alert=True)
        return
    pending = (await state.get_data()).get("pending_client_message")
    ticket = await tickets_repo.get_ticket(session, ticket_id)
    if await service.is_staff(session, author.id):
        await query.answer("Сотрудникам недоступен клиентский чат", show_alert=True)
        return
    if (
        not isinstance(pending, dict)
        or ticket is None
        or ticket.telegram_user_id != author.id
        or ticket.state == "closed"
    ):
        await query.answer("Выбор устарел. Отправьте сообщение ещё раз.", show_alert=True)
        return
    result = await service.relay_from_client(
        session,
        bot,
        ticket_id=ticket_id,
        text=pending.get("text") or "📷 Фото",
        photo_file_id=pending.get("photo_file_id"),
        card_markup=manager_notification_keyboard,
    )
    await session.commit()
    if result.ok:
        source_chat_id = pending.get("source_chat_id")
        source_message_id = pending.get("source_message_id")
        if isinstance(source_chat_id, int) and isinstance(source_message_id, int):
            await _ui(clock).remove_incoming(bot, source_chat_id, source_message_id)
    await state.clear()
    if result.ok:
        await query.answer("Сообщение добавлено")
        await _render_ticket(session, bot, ticket_id=ticket_id, viewer_id=author.id, clock=clock)
        await state.update_data(ticket_id=ticket_id)
    else:
        await query.answer("Сообщение сохранено, но не доставлено", show_alert=True)
        await _ui(clock).render(
            session,
            bot,
            chat_id=author.id,
            parts=["Сообщение сохранено, но менеджерам пока не удалось его доставить."],
            reply_markup=chat_keyboard(writable=False, back_data="sup:tickets"),
        )


@router.message(ClientFlow.choosing_ticket)
async def keep_pending_message_while_choosing(
    message: Message, state: FSMContext, session: AsyncSession, bot: Bot, clock: Clock
) -> None:
    pending = (await state.get_data()).get("pending_client_message")
    if not isinstance(pending, dict):
        return
    await _render_pending_ticket_choice(
        session,
        bot,
        chat_id=message.chat.id,
        state=state,
        clock=clock,
        prompt=(
            "Предыдущее сообщение сохранено. Новое сообщение пока не отправлено; "
            "после выбора чата отправьте его повторно."
        ),
    )


@router.message(StateFilter(None), F.text)
async def handle_client_message(
    message: Message,
    service: TicketService,
    state: FSMContext | None = None,
    session: AsyncSession | None = None,
    bot: Bot | None = None,
    clock: Clock | None = None,
) -> None:
    if state is not None and session is not None and bot is not None and clock is not None:
        await _route_unprompted_message(message, state, session, bot, service, clock)
        return
    author = message.from_user
    if author is not None and not service.is_manager(author.id) and not service.is_admin(author.id):
        await message.answer("Откройте /start, затем «Мои обращения» и нужный чат.")


@router.message(ClientFlow.waiting_category)
async def handle_wrong_in_category(message: Message) -> None:
    await message.answer("Выберите категорию кнопкой выше.")


@router.message(ClientFlow.waiting_description)
async def handle_non_text_description(message: Message) -> None:
    await message.answer("Опишите вопрос текстом — фото и файлы пока не передаются.")


@router.message(StateFilter(None), ~F.text)
async def handle_client_non_text(
    message: Message,
    service: TicketService,
    state: FSMContext | None = None,
    session: AsyncSession | None = None,
    bot: Bot | None = None,
    clock: Clock | None = None,
) -> None:
    if state is not None and session is not None and bot is not None and clock is not None:
        await _route_unprompted_message(message, state, session, bot, service, clock)
        return
    author = message.from_user
    if author is not None and not service.is_manager(author.id) and not service.is_admin(author.id):
        await message.answer("Фото и файлы менеджеру не передаются. Опишите вопрос текстом.")
