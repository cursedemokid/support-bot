"""Менеджерский сценарий: очередь, взятие, закрытие, ответ клиенту.

Карточка нового тикета приходит в чат менеджеров с кнопками «Взять» и
«Закрыть». Ответить клиенту — значит прислать reply на карточку или на любое
сообщение клиента в этом чате: тикет определяется по message_id, на который
ответили (support.message_routes).
"""

from __future__ import annotations

from aiogram import Bot, F, Router, html
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import callbacks
from app.bot.filters.access import IsManager
from app.bot.keyboards import (
    manager_chat_keyboard,
    manager_menu,
    product_keyboard,
    queue_keyboard,
    with_back,
)
from app.catalog import product_card_parts
from app.clock import Clock
from app.config import AppConfig
from app.db.models import MessageHistory
from app.media import resolve_media
from app.repositories import content as content_repo
from app.repositories import messages as messages_repo
from app.repositories import staff as staff_repo
from app.repositories import tickets as tickets_repo
from app.services.tickets import ActionResult, TicketService, category_title
from app.services.ui import UiPhoto, UiService

router = Router(name="manager_commands")

# Только менеджеры: команду в личке может набрать любой, кто знает бота.
# ЭКЗЕМПЛЯР, а не класс: aiogram вызывает фильтр, как функцию, и `IsManager()`
# вместо `IsManager` — не придирка стиля, а разница между «права проверены»
# и `TypeError: IsManager() takes no arguments` на каждом апдейте.
router.message.filter(IsManager())
router.callback_query.filter(IsManager())


class ManagerChatFlow(StatesGroup):
    writing_ticket = State()


@router.message(CommandStart())
async def manager_start(
    message: Message, session: AsyncSession, bot: Bot, clock: Clock, service: TicketService
) -> None:
    assert message.from_user is not None
    await UiService(clock).remove_incoming(bot, message.chat.id, message.message_id)
    await UiService(clock).render(
        session,
        bot,
        chat_id=message.from_user.id,
        parts=["Личный кабинет сотрудника."],
        reply_markup=manager_menu(
            admin=await service.is_database_admin(session, message.from_user.id)
        ),
    )


def _short(text: str, limit: int = 52) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else f"{text[: limit - 1]}…"


def _history_parts(
    history: list[MessageHistory], viewer_id: int, clock: Clock
) -> list[str | UiPhoto]:
    parts: list[str | UiPhoto] = []
    day = None
    for item in history:
        local = item.sent_at.astimezone(clock.tz)
        if local.date() != day:
            day = local.date()
            parts.append(f"<b>──────── {day:%d.%m.%Y} ────────</b>")
        author = "Вы" if item.from_user_id == viewer_id else "Клиент"
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


async def _render_manager_list(
    session: AsyncSession,
    bot: Bot,
    manager_id: int,
    clock: Clock,
    service: TicketService,
    *,
    state: str,
) -> None:
    if state == "new":
        tickets, title = await tickets_repo.list_unassigned(session), "Открытые обращения"
    elif state == "assigned":
        tickets, title = await tickets_repo.list_assigned_to(session, manager_id), "В работе"
    elif await service.is_database_admin(session, manager_id):
        tickets, title = await tickets_repo.list_closed_tickets(session), "История обращений"
    else:
        tickets = await tickets_repo.list_closed_for_staff(session, manager_id)
        title = "История обращений"
    markup = with_back(
        "mgrtkt",
        [(ticket.id, f"{_status_icon(ticket.state)} {_short(ticket.title)}") for ticket in tickets],
        "mgr:home",
    )
    await UiService(clock).render(
        session,
        bot,
        chat_id=manager_id,
        parts=[f"<b>{title}</b>" if tickets else f"<b>{title}</b>\n\nПока пусто."],
        reply_markup=markup,
    )


async def _render_manager_ticket(
    session: AsyncSession,
    bot: Bot,
    manager_id: int,
    ticket_id: int,
    clock: Clock,
    service: TicketService,
) -> bool:
    ticket = await tickets_repo.get_ticket(session, ticket_id)
    if ticket is None:
        return False
    is_admin = await service.is_database_admin(session, manager_id)
    if ticket.state == "assigned" and ticket.manager_id != manager_id:
        return False
    if (
        ticket.state == "closed"
        and not is_admin
        and not await tickets_repo.was_assigned_to(
            session, ticket_id=ticket.id, telegram_user_id=manager_id
        )
    ):
        return False
    history = await messages_repo.get_history(session, ticket.id)
    writable = ticket.state != "closed" and ticket.manager_id in (None, manager_id)
    await UiService(clock).render(
        session,
        bot,
        chat_id=manager_id,
        parts=_history_parts(history, manager_id, clock),
        reply_markup=manager_chat_keyboard(
            writable=writable,
            take_ticket_id=ticket.id if ticket.state == "new" else None,
            transfer_ticket_id=(
                ticket.id
                if ticket.state == "assigned" and ticket.manager_id == manager_id
                else None
            ),
            close_ticket_id=ticket.id if writable else None,
        ),
    )
    return True


@router.callback_query(F.data == "mgr:home")
async def manager_home(
    query: CallbackQuery,
    bot: Bot,
    session: AsyncSession,
    clock: Clock,
    service: TicketService,
    state: FSMContext,
) -> None:
    await state.clear()
    await query.answer()
    await UiService(clock).render(
        session,
        bot,
        chat_id=query.from_user.id,
        parts=["Личный кабинет сотрудника."],
        reply_markup=manager_menu(
            admin=await service.is_database_admin(session, query.from_user.id)
        ),
    )


@router.message(Command("queue"))
async def handle_queue(
    message: Message,
    session: AsyncSession,
    service: TicketService,
    config: AppConfig,
) -> None:
    tickets = await service.list_open(session)
    if not tickets:
        await message.answer("Очередь пуста — открытых обращений нет.")
        return

    lines = ["<b>Открытые обращения</b>", ""]
    buttons: list[tuple[int, str]] = []
    for ticket in tickets:
        who = (
            f"@{ticket.telegram_username}"
            if ticket.telegram_username
            else f"id {ticket.telegram_user_id}"
        )
        holder = "🟡 в работе" if ticket.state == "assigned" else "🆕 новое"
        lines.append(f"№{ticket.id} · {category_title(config, ticket.category)} · {holder}\n{who}")
        if ticket.state == "new":
            buttons.append((ticket.id, f"Взять №{ticket.id}"))

    markup = queue_keyboard(buttons) if buttons else None
    await message.answer("\n\n".join(lines), reply_markup=markup)


@router.callback_query(F.data == "mgr:open")
async def handle_open_button(
    query: CallbackQuery,
    session: AsyncSession,
    bot: Bot,
    clock: Clock,
    state: FSMContext,
    service: TicketService,
) -> None:
    await state.clear()
    await query.answer()
    await _render_manager_list(session, bot, query.from_user.id, clock, service, state="new")


@router.callback_query(F.data == "mgr:assigned")
async def handle_assigned_button(
    query: CallbackQuery,
    session: AsyncSession,
    bot: Bot,
    clock: Clock,
    state: FSMContext,
    service: TicketService,
) -> None:
    await state.clear()
    await query.answer()
    await _render_manager_list(session, bot, query.from_user.id, clock, service, state="assigned")


@router.callback_query(F.data == "mgr:catalog")
async def manager_catalog(
    query: CallbackQuery, session: AsyncSession, bot: Bot, clock: Clock
) -> None:
    products = await content_repo.find_products(session, "")
    await query.answer()
    await UiService(clock).render(
        session,
        bot,
        chat_id=query.from_user.id,
        parts=["<b>Каталог товаров</b>\n\nВыберите товар для просмотра инструкции."],
        reply_markup=with_back(
            "prod",
            [(product.id, f"{product.sku} · {product.title}") for product in products],
            "mgr:home",
        ),
    )


@router.callback_query(F.data.startswith("prod:"))
async def manager_product(
    query: CallbackQuery, session: AsyncSession, bot: Bot, clock: Clock
) -> None:
    try:
        product_id = int((query.data or "").split(":", 1)[1])
    except ValueError:
        product_id = 0
    product = await content_repo.product(session, product_id)
    if product is None:
        await query.answer("Товар не найден", show_alert=True)
        return
    images = await content_repo.product_images(session, product.id)
    await query.answer()
    await UiService(clock).render(
        session,
        bot,
        chat_id=query.from_user.id,
        parts=product_card_parts(product, [image.file_id for image in images]),
        reply_markup=product_keyboard(product, back_data="mgr:catalog"),
    )


@router.callback_query(F.data.startswith("pdf:"))
async def manager_product_pdf(query: CallbackQuery, session: AsyncSession, bot: Bot) -> None:
    try:
        product_id = int((query.data or "").split(":", 1)[1])
    except ValueError:
        product_id = 0
    product = await content_repo.product(session, product_id)
    if product is None:
        await query.answer("Инструкция не найдена", show_alert=True)
        return
    await bot.send_document(
        query.from_user.id,
        resolve_media(product.pdf_file_id),
        caption=f"Инструкция: {html.quote(product.title)}",
    )
    await query.answer("Инструкция отправлена")


@router.callback_query(F.data == "mgr:history")
async def handle_history_button(
    query: CallbackQuery,
    session: AsyncSession,
    bot: Bot,
    clock: Clock,
    state: FSMContext,
    service: TicketService,
) -> None:
    await state.clear()
    await query.answer()
    await _render_manager_list(session, bot, query.from_user.id, clock, service, state="closed")


@router.callback_query(F.data.startswith("mgrtkt:"))
async def open_manager_ticket(
    query: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
    clock: Clock,
    service: TicketService,
) -> None:
    try:
        ticket_id = int((query.data or "").split(":", 1)[1])
    except ValueError:
        await query.answer("Обращение не найдено", show_alert=True)
        return
    if not await _render_manager_ticket(
        session, bot, query.from_user.id, ticket_id, clock, service
    ):
        await query.answer("Обращение не найдено", show_alert=True)
        return
    await state.update_data(ticket_id=ticket_id)
    await query.answer()


@router.callback_query(F.data == "mgr:list")
async def return_manager_list(
    query: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
    clock: Clock,
    service: TicketService,
) -> None:
    await state.clear()
    await query.answer()
    await _render_manager_list(session, bot, query.from_user.id, clock, service, state="new")


@router.callback_query(F.data.startswith("mgrchat:take:"))
async def take_from_chat(
    query: CallbackQuery,
    state: FSMContext,
    bot: Bot,
    session: AsyncSession,
    service: TicketService,
    clock: Clock,
) -> None:
    try:
        ticket_id = int((query.data or "").rsplit(":", 1)[1])
    except ValueError:
        await query.answer("Обращение не найдено", show_alert=True)
        return
    result = await service.take(session, bot, ticket_id=ticket_id, manager_id=query.from_user.id)
    await session.commit()
    if not result.ok:
        await query.answer(_done(result), show_alert=True)
        return
    await state.update_data(ticket_id=ticket_id)
    await _render_manager_ticket(session, bot, query.from_user.id, ticket_id, clock, service)
    await query.answer("Взято в работу")


@router.callback_query(F.data.startswith("mgrchat:transfer:"))
async def choose_transfer_target(
    query: CallbackQuery, session: AsyncSession, bot: Bot, clock: Clock
) -> None:
    try:
        ticket_id = int((query.data or "").rsplit(":", 1)[1])
    except ValueError:
        await query.answer("Обращение не найдено", show_alert=True)
        return
    ticket = await tickets_repo.get_ticket(session, ticket_id)
    if ticket is None or ticket.manager_id != query.from_user.id or ticket.state != "assigned":
        await query.answer("Передача недоступна", show_alert=True)
        return
    targets = [
        (member.telegram_user_id, member.display_name)
        for member in await staff_repo.active_staff(session)
        if member.telegram_user_id is not None and member.telegram_user_id != query.from_user.id
    ]
    if not targets:
        await query.answer("Нет доступных сотрудников", show_alert=True)
        return
    await query.answer()
    await UiService(clock).render(
        session,
        bot,
        chat_id=query.from_user.id,
        parts=["Кому передать обращение?"],
        reply_markup=with_back(
            "mgrxfer",
            [(f"{ticket_id}:{target_id}", name) for target_id, name in targets],
            "mgr:assigned",
        ),
    )


@router.callback_query(F.data.regexp(r"^mgrxfer:\d+:\d+$"))
async def transfer_ticket(
    query: CallbackQuery,
    session: AsyncSession,
    bot: Bot,
    service: TicketService,
) -> None:
    _, ticket_id_raw, target_id_raw = (query.data or "").split(":")
    result = await service.transfer(
        session,
        bot,
        ticket_id=int(ticket_id_raw),
        from_manager_id=query.from_user.id,
        to_manager_id=int(target_id_raw),
    )
    await session.commit()
    await query.answer("Передано" if result.ok else _done(result), show_alert=not result.ok)


@router.callback_query(F.data.startswith("mgrchat:close:"))
async def close_from_chat(
    query: CallbackQuery, session: AsyncSession, bot: Bot, service: TicketService
) -> None:
    try:
        ticket_id = int((query.data or "").rsplit(":", 1)[1])
    except ValueError:
        await query.answer("Обращение не найдено", show_alert=True)
        return
    result = await service.close(session, bot, ticket_id=ticket_id, manager_id=query.from_user.id)
    await session.commit()
    await query.answer("Закрыто" if result.ok else _done(result), show_alert=not result.ok)


@router.callback_query(F.data == "mgrchat:write")
async def begin_manager_writing(
    query: CallbackQuery, state: FSMContext, session: AsyncSession, bot: Bot, clock: Clock
) -> None:
    ticket_id = (await state.get_data()).get("ticket_id")
    if not isinstance(ticket_id, int):
        await query.answer("Обращение не найдено", show_alert=True)
        return
    await _begin_manager_writing(query, state, session, bot, clock, ticket_id)


@router.callback_query(F.data.regexp(r"^mgrreply:\d+$"))
async def begin_notification_reply(
    query: CallbackQuery, state: FSMContext, session: AsyncSession, bot: Bot, clock: Clock
) -> None:
    ticket_id = int((query.data or "").split(":", 1)[1])
    await state.clear()
    await state.update_data(ticket_id=ticket_id)
    await _begin_manager_writing(query, state, session, bot, clock, ticket_id)


async def _begin_manager_writing(
    query: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
    clock: Clock,
    ticket_id: int,
) -> None:
    ticket = await tickets_repo.get_ticket(session, ticket_id)
    if (
        ticket is None
        or ticket.state == "closed"
        or ticket.manager_id not in (None, query.from_user.id)
    ):
        await query.answer("В это обращение нельзя написать", show_alert=True)
        return
    history = await messages_repo.get_history(session, ticket.id)
    await state.set_state(ManagerChatFlow.writing_ticket)
    await query.answer()
    await UiService(clock).render(
        session,
        bot,
        chat_id=query.from_user.id,
        parts=[
            *_history_parts(history, query.from_user.id, clock),
            "<i>Напишите ответ клиенту текстом или отправьте фотографию.</i>",
        ],
        reply_markup=manager_chat_keyboard(writable=False, back_data="mgrchat:read"),
    )


@router.callback_query(F.data == "mgrchat:read")
async def return_manager_reading(
    query: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
    clock: Clock,
    service: TicketService,
) -> None:
    ticket_id = (await state.get_data()).get("ticket_id")
    await state.clear()
    if not isinstance(ticket_id, int) or not await _render_manager_ticket(
        session, bot, query.from_user.id, ticket_id, clock, service
    ):
        await query.answer("Обращение не найдено", show_alert=True)
        return
    await state.update_data(ticket_id=ticket_id)
    await query.answer()


@router.message(ManagerChatFlow.writing_ticket, F.text)
async def append_manager_message(
    message: Message,
    state: FSMContext,
    bot: Bot,
    session: AsyncSession,
    service: TicketService,
    clock: Clock,
) -> None:
    await _append_manager_message(message, state, bot, session, service, clock)


@router.message(ManagerChatFlow.writing_ticket, F.photo)
async def append_manager_photo(
    message: Message,
    state: FSMContext,
    bot: Bot,
    session: AsyncSession,
    service: TicketService,
    clock: Clock,
) -> None:
    await _append_manager_message(message, state, bot, session, service, clock)


async def _append_manager_message(
    message: Message,
    state: FSMContext,
    bot: Bot,
    session: AsyncSession,
    service: TicketService,
    clock: Clock,
) -> None:
    ticket_id = (await state.get_data()).get("ticket_id")
    text = (message.text or message.caption or "").strip()
    photo_file_id = message.photo[-1].file_id if message.photo else None
    if (
        not isinstance(ticket_id, int)
        or (not text and photo_file_id is None)
        or message.from_user is None
    ):
        return
    result = await service.relay_from_manager(
        session,
        bot,
        ticket_id=ticket_id,
        manager_id=message.from_user.id,
        text=text or "📷 Фото",
        photo_file_id=photo_file_id,
    )
    await session.commit()
    await UiService(clock).remove_incoming(bot, message.chat.id, message.message_id)
    await state.clear()
    if not result.ok:
        await UiService(clock).render(
            session,
            bot,
            chat_id=message.from_user.id,
            parts=[f"Сообщение не доставлено: {result.reason or 'попробуйте позже.'}"],
            reply_markup=manager_chat_keyboard(writable=False),
        )
        return
    await _render_manager_ticket(session, bot, message.from_user.id, ticket_id, clock, service)
    await state.update_data(ticket_id=ticket_id)


@router.message(Command("take"))
async def handle_take(
    message: Message,
    command: CommandObject,
    bot: Bot,
    session: AsyncSession,
    service: TicketService,
) -> None:
    ticket_id = _parse_id(command.args)
    if ticket_id is None:
        await message.answer("Формат: /take <номер обращения>, например /take 42")
        return

    assert message.from_user is not None  # гарантирован фильтром IsManager
    result = await service.take(session, bot, ticket_id=ticket_id, manager_id=message.from_user.id)
    await session.commit()
    await message.answer(f"№{ticket_id} — {_done(result)}")


@router.message(Command("close"))
async def handle_close(
    message: Message,
    command: CommandObject,
    bot: Bot,
    session: AsyncSession,
    service: TicketService,
) -> None:
    ticket_id = _parse_id(command.args)
    if ticket_id is None:
        await message.answer("Формат: /close <номер обращения>, например /close 42")
        return

    assert message.from_user is not None  # гарантирован фильтром IsManager
    result = await service.close(session, bot, ticket_id=ticket_id, manager_id=message.from_user.id)
    await session.commit()
    await message.answer(f"№{ticket_id} — {_done(result)}")


@router.callback_query(callbacks.Callback(callbacks.TakeTicket))
async def handle_take_button(
    query: CallbackQuery,
    callback_data: callbacks.TakeTicket,
    bot: Bot,
    session: AsyncSession,
    service: TicketService,
) -> None:
    result = await service.take(
        session, bot, ticket_id=callback_data.ticket_id, manager_id=query.from_user.id
    )
    await session.commit()
    await query.answer(_done(result), show_alert=not result.ok)


@router.callback_query(callbacks.Callback(callbacks.CloseTicket))
async def handle_close_button(
    query: CallbackQuery,
    callback_data: callbacks.CloseTicket,
    bot: Bot,
    session: AsyncSession,
    service: TicketService,
) -> None:
    result = await service.close(
        session, bot, ticket_id=callback_data.ticket_id, manager_id=query.from_user.id
    )
    await session.commit()
    await query.answer(_done(result), show_alert=not result.ok)


@router.message(F.chat.type == "private", F.reply_to_message)
async def handle_manager_reply(
    message: Message,
    bot: Bot,
    session: AsyncSession,
    service: TicketService,
) -> None:
    """Ответ менеджера: reply на карточку тикета или на сообщение клиента."""
    replied = message.reply_to_message
    text = (message.text or "").strip()
    if replied is None or not text:
        return

    ticket_id = await service.resolve_ticket_id(
        session, chat_id=message.chat.id, message_id=replied.message_id
    )
    if ticket_id is None:
        await message.reply(
            "Не понял, к какому обращению это относится — ответьте на карточку тикета."
        )
        return

    assert message.from_user is not None  # гарантирован фильтром IsManager
    result = await service.relay_from_manager(
        session, bot, ticket_id=ticket_id, manager_id=message.from_user.id, text=text
    )
    await session.commit()
    if result.ok:
        await message.reply(f"Передано клиенту по обращению №{ticket_id}.")
    else:
        await message.reply(f"Не доставлено: {result.reason}")


def _parse_id(raw: str | None) -> int | None:
    if not raw:
        return None
    try:
        value = int(raw.strip().split()[0])
    except (ValueError, IndexError):
        return None
    return value if value > 0 else None


def _done(result: ActionResult) -> str:
    """Результат действия менеджера — одной строкой для чата."""
    if result.ok:
        return "готово."
    return result.reason or "не удалось."


def _status_icon(state: str) -> str:
    return {"new": "🆕", "assigned": "🟡", "closed": "⚫"}.get(state, "•")
