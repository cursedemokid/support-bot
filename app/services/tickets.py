"""Сервис тикетов: бизнес-логика и рассылка уведомлений.

Хендлер разбирает `aiogram.types.Message` и решает, что ответить; вся работа
с базой и переписка между клиентом и менеджером — здесь. Репозитории про
уведомления не знают, сервис не знает про фильтры и FSM.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from aiogram import Bot, html
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import InlineKeyboardMarkup
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.clock import Clock
from app.config import AppConfig, CategoryConfig
from app.db.models import Ticket
from app.domain import TicketState
from app.logging import log_event
from app.repositories import content as content_repo
from app.repositories import messages as messages_repo
from app.repositories import routes as routes_repo
from app.repositories import staff as staff_repo
from app.repositories import tickets as tickets_repo
from app.services.ui import TELEGRAM_CAPTION_LIMIT, _split, telegram_length
from app.ticket_titles import make_ticket_title


@dataclass(frozen=True, slots=True)
class CreatedTicket:
    ticket: Ticket
    notified: bool  # ушла ли карточка в чат менеджеров


@dataclass(frozen=True, slots=True)
class ActionResult:
    ok: bool
    reason: str | None = None  # человекочитаемая причина отказа


def category_of(config: AppConfig, key: str) -> CategoryConfig | None:
    """Категория по ключу из callback_data. None — такой в конфиге нет."""
    for category in config.settings.support.categories:
        if category.key == key:
            return category
    return None


def category_title(config: AppConfig, key: str) -> str:
    """Заголовок категории для показа. Незнакомый ключ печатается как есть."""
    category = category_of(config, key)
    return category.title if category is not None else key


class TicketService:
    """Работа с тикетами: создание, взятие, закрытие, пересылка ответов."""

    def __init__(self, config: AppConfig, clock: Clock) -> None:
        self._config = config
        self._clock = clock

    @property
    def _manager_ids(self) -> tuple[int, ...]:
        return self._config.settings.support.manager_user_ids

    def is_manager(self, telegram_user_id: int) -> bool:
        return telegram_user_id in self._config.settings.support.manager_user_ids

    def is_admin(self, telegram_user_id: int) -> bool:
        return telegram_user_id in self._config.settings.support.admin_user_ids

    async def is_staff(self, session: AsyncSession, telegram_user_id: int) -> bool:
        return (
            self.is_manager(telegram_user_id)
            or self.is_admin(telegram_user_id)
            or (await staff_repo.active_by_telegram_id(session, telegram_user_id) is not None)
        )

    async def is_database_admin(self, session: AsyncSession, telegram_user_id: int) -> bool:
        member = await staff_repo.active_by_telegram_id(session, telegram_user_id)
        return self.is_admin(telegram_user_id) or (
            member is not None and member.role == staff_repo.ADMIN
        )

    async def create_ticket(
        self,
        session: AsyncSession,
        bot: Bot,
        *,
        telegram_user_id: int,
        telegram_username: str | None,
        category_key: str,
        description: str,
        photo_file_id: str | None = None,
        card_markup: Callable[[int], InlineKeyboardMarkup] | None = None,
    ) -> CreatedTicket:
        """Создать тикет и отправить карточку в чат менеджеров.

        Тикет создаётся всегда, даже если карточку отправить не удалось:
        обращение клиента уже принято, и терять его из-за недоступного чата
        нельзя. `notified=False` говорит хендлеру, что менеджеры не в курсе.

        `card_markup` — фабрика «id тикета → клавиатура», её даёт вызывающий:
        клавиатуры принадлежат слою bot, а id появляется только здесь, после
        вставки в базу. Сервис про кнопки не знает и знать не должен
        (design.md §1).
        """
        category_title_value = await self._category_title(session, category_key)
        ticket = await tickets_repo.create_ticket(
            session,
            telegram_user_id=telegram_user_id,
            telegram_username=telegram_username,
            category=category_key,
            description=description,
            title=make_ticket_title(category_title_value, description),
            created_at=self._clock.now(),
        )
        await messages_repo.add_message(
            session,
            ticket_id=ticket.id,
            from_user_id=telegram_user_id,
            message_text=description,
            sent_at=self._clock.now(),
            photo_file_id=photo_file_id,
        )
        log_event(
            "INFO",
            "Новый тикет",
            action="ticket.create",
            ticket_id=ticket.id,
            category=category_key,
            telegram_user_id=telegram_user_id,
            state=ticket.state,
        )

        notified = await self._send_card_to_managers(
            session, bot, ticket, card_markup, photo_file_id=photo_file_id
        )
        return CreatedTicket(ticket=ticket, notified=notified)

    async def take(
        self,
        session: AsyncSession,
        bot: Bot,
        *,
        ticket_id: int,
        manager_id: int,
    ) -> ActionResult:
        ticket = await tickets_repo.get_ticket(session, ticket_id)
        if ticket is None:
            return ActionResult(ok=False, reason="Тикет не найден.")
        if ticket.state == TicketState.CLOSED.value:
            return ActionResult(ok=False, reason="Тикет уже закрыт.")
        if ticket.state == TicketState.ASSIGNED.value:
            if ticket.manager_id == manager_id:
                return ActionResult(ok=False, reason="Ты уже ведёшь этот тикет.")
            return ActionResult(ok=False, reason="Тикет уже взял другой менеджер.")

        assigned = await tickets_repo.assign_ticket(
            session, ticket_id=ticket_id, manager_id=manager_id, assigned_at=self._clock.now()
        )
        if assigned is None:
            return ActionResult(ok=False, reason="Тикет уже взял другой менеджер.")
        log_event(
            "INFO",
            "Тикет взят в работу",
            action="ticket.take",
            ticket_id=ticket_id,
            manager_id=manager_id,
            state=TicketState.ASSIGNED.value,
        )
        # Карточка обновится при следующем открытии чата. Не создаём отдельное
        # уведомление клиенту: личный интерфейс должен оставаться чистым.
        return ActionResult(ok=True)

    async def close(
        self,
        session: AsyncSession,
        bot: Bot,
        *,
        ticket_id: int,
        manager_id: int,
    ) -> ActionResult:
        ticket = await tickets_repo.get_ticket(session, ticket_id)
        if ticket is None:
            return ActionResult(ok=False, reason="Тикет не найден.")
        if ticket.state == TicketState.CLOSED.value:
            return ActionResult(ok=False, reason="Тикет уже закрыт.")
        if ticket.manager_id is not None and ticket.manager_id != manager_id:
            return ActionResult(ok=False, reason="Тикет ведёт другой менеджер.")

        if ticket.state == TicketState.NEW.value:
            assigned = await tickets_repo.assign_ticket(
                session,
                ticket_id=ticket_id,
                manager_id=manager_id,
                assigned_at=self._clock.now(),
            )
            if assigned is None:
                return ActionResult(ok=False, reason="Тикет уже взял другой менеджер.")

        closed = await tickets_repo.close_ticket(
            session,
            ticket_id=ticket_id,
            closed_at=self._clock.now(),
            manager_id=manager_id,
        )
        if closed is None:
            return ActionResult(ok=False, reason="Обращение уже передано или закрыто.")
        log_event(
            "INFO",
            "Тикет закрыт",
            action="ticket.close",
            ticket_id=ticket_id,
            manager_id=manager_id,
            state=TicketState.CLOSED.value,
        )
        # По продуктовой политике закрытие без ответа не уведомляет клиента.
        return ActionResult(ok=True)

    async def relay_from_manager(
        self,
        session: AsyncSession,
        bot: Bot,
        *,
        ticket_id: int,
        manager_id: int,
        text: str,
        photo_file_id: str | None = None,
    ) -> ActionResult:
        """Ответ менеджера → клиенту в личку."""
        ticket = await tickets_repo.get_ticket(session, ticket_id)
        if ticket is None:
            return ActionResult(ok=False, reason="Тикет не найден.")
        if ticket.state == TicketState.CLOSED.value:
            return ActionResult(ok=False, reason="Тикет закрыт — отвечать некому.")
        if ticket.state == TicketState.ASSIGNED.value and ticket.manager_id != manager_id:
            return ActionResult(ok=False, reason="Обращение уже ведёт другой сотрудник.")

        # Отвечающий становится ведущим: ответ «я возьму» не требуется, иначе
        # между «беру» и «отвечаю» клиент ждал бы лишний шаг.
        if ticket.state == TicketState.NEW.value:
            assigned = await tickets_repo.assign_ticket(
                session,
                ticket_id=ticket_id,
                manager_id=manager_id,
                assigned_at=self._clock.now(),
            )
            if assigned is None:
                return ActionResult(ok=False, reason="Обращение уже ведёт другой сотрудник.")

        await messages_repo.add_message(
            session,
            ticket_id=ticket_id,
            from_user_id=manager_id,
            message_text=text,
            sent_at=self._clock.now(),
            photo_file_id=photo_file_id,
        )
        response = (
            f"💬 <b>Вам ответили по обращению «{html.quote(ticket.title)}»</b>\n\n"
            f"{html.quote(text)}"
        )
        try:
            await self._send_routed_message(
                session,
                bot,
                ticket,
                ticket.telegram_user_id,
                response,
                photo_file_id=photo_file_id,
            )
        except (TelegramForbiddenError, TelegramBadRequest) as exc:
            logger.warning("Ответ по тикету {} не доставлен: {}", ticket_id, exc)
            return ActionResult(
                ok=False,
                reason="Клиент закрыл личку с ботом — сообщение не доставлено.",
            )

        log_event(
            "INFO",
            "Ответ менеджера доставлен",
            action="ticket.reply",
            ticket_id=ticket_id,
            manager_id=manager_id,
        )
        return ActionResult(ok=True)

    async def relay_from_client(
        self,
        session: AsyncSession,
        bot: Bot,
        *,
        ticket_id: int,
        text: str,
        photo_file_id: str | None = None,
        card_markup: Callable[[int], InlineKeyboardMarkup] | None = None,
    ) -> ActionResult:
        """Сообщение клиента по открытому тикету → в чат менеджеров."""
        ticket = await tickets_repo.get_ticket(session, ticket_id)
        if ticket is None:
            return ActionResult(ok=False, reason="Тикет не найден.")
        if ticket.state == TicketState.CLOSED.value:
            return ActionResult(ok=False, reason="Обращение закрыто. Создайте новое через /start.")

        await messages_repo.add_message(
            session,
            ticket_id=ticket_id,
            from_user_id=ticket.telegram_user_id,
            message_text=text,
            sent_at=self._clock.now(),
            photo_file_id=photo_file_id,
        )

        title = await self._category_title(session, ticket.category)
        header = (
            f"💬 <b>Тикет №{ticket_id}</b> · {html.quote(title)}\n"
            f"от {html.quote(self._sender_label(ticket))}:"
        )
        delivered = await self._send_to_managers(
            session,
            bot,
            ticket,
            f"{header}\n\n{html.quote(text)}",
            card_markup(ticket.id) if card_markup is not None else None,
            photo_file_id=photo_file_id,
            recipient_ids=(ticket.manager_id,) if ticket.manager_id is not None else None,
        )
        return ActionResult(ok=delivered, reason=None if delivered else "Менеджеры недоступны.")

    async def transfer(
        self,
        session: AsyncSession,
        bot: Bot,
        *,
        ticket_id: int,
        from_manager_id: int,
        to_manager_id: int,
    ) -> ActionResult:
        target = await staff_repo.active_by_telegram_id(session, to_manager_id)
        configured = (*self._manager_ids, *self._config.settings.support.admin_user_ids)
        if target is None and to_manager_id not in configured:
            return ActionResult(ok=False, reason="Сотрудник недоступен.")
        ticket = await tickets_repo.transfer_ticket(
            session,
            ticket_id=ticket_id,
            from_manager_id=from_manager_id,
            to_manager_id=to_manager_id,
            transferred_at=self._clock.now(),
        )
        if ticket is None:
            return ActionResult(ok=False, reason="Обращение уже передано или закрыто.")
        try:
            await bot.send_message(
                to_manager_id,
                f"📬 Вам передано обращение «{html.quote(ticket.title)}».",
            )
        except (TelegramForbiddenError, TelegramBadRequest) as exc:
            logger.warning("Передача тикета {} не доставлена {}: {}", ticket.id, to_manager_id, exc)
        return ActionResult(ok=True)

    async def list_open(self, session: AsyncSession) -> list[Ticket]:
        """Открытые тикеты для очереди менеджера."""
        return await tickets_repo.list_open_tickets(session)

    async def list_closed(self, session: AsyncSession) -> list[Ticket]:
        return await tickets_repo.list_closed_tickets(session)

    async def list_all(self, session: AsyncSession) -> list[Ticket]:
        return await tickets_repo.list_all(session)

    async def resolve_ticket_id(
        self, session: AsyncSession, *, chat_id: int, message_id: int
    ) -> int | None:
        """Тикет по сообщению, на которое ответили. None — сообщение не наше."""
        return await routes_repo.resolve(session, chat_id=chat_id, message_id=message_id)

    async def ticket_for_reply(
        self, session: AsyncSession, *, telegram_user_id: int
    ) -> Ticket | None:
        """Открытый тикет клиента, если он один. Иначе None.

        Клиент отвечает в личке простым сообщением, без reply: привязку даёт
        единственный открытый тикет. Несколько открытых — привязки нет, и это
        осознанно: угадывать, к какому из двух обращений относится сообщение,
        значит однажды перепутать.
        """
        open_tickets = await tickets_repo.list_open_for_user(session, telegram_user_id)
        if len(open_tickets) == 1:
            return open_tickets[0]
        return None

    @staticmethod
    def _sender_label(ticket: Ticket) -> str:
        if ticket.telegram_username:
            return f"@{ticket.telegram_username}"
        return f"id {ticket.telegram_user_id}"

    async def _send_card_to_managers(
        self,
        session: AsyncSession,
        bot: Bot,
        ticket: Ticket,
        card_markup: Callable[[int], InlineKeyboardMarkup] | None = None,
        *,
        photo_file_id: str | None = None,
    ) -> bool:
        title = await self._category_title(session, ticket.category)
        card = (
            f"🆕 <b>Тикет №{ticket.id}</b>\n"
            f"Категория: {html.quote(title)}\n"
            f"Клиент: {html.quote(self._sender_label(ticket))}\n\n"
            f"{html.quote(ticket.description)}"
        )
        markup = card_markup(ticket.id) if card_markup is not None else None
        return await self._send_to_managers(
            session, bot, ticket, card, markup, photo_file_id=photo_file_id
        )

    async def _send_to_managers(
        self,
        session: AsyncSession,
        bot: Bot,
        ticket: Ticket,
        text: str,
        markup: InlineKeyboardMarkup | None,
        *,
        photo_file_id: str | None = None,
        recipient_ids: tuple[int | None, ...] | None = None,
    ) -> bool:
        delivered = False
        if recipient_ids is None:
            staff = await staff_repo.active_staff(session)
            candidates = (
                *self._manager_ids,
                *self._config.settings.support.admin_user_ids,
                *(member.telegram_user_id for member in staff),
            )
            # dict сохраняет стабильный порядок и одновременно исключает
            # повтор, если сотрудник также указан в bootstrap-конфиге.
            recipient_ids = tuple(dict.fromkeys(candidates))
        for manager_id in (value for value in recipient_ids if value is not None):
            try:
                await self._send_routed_message(
                    session,
                    bot,
                    ticket,
                    manager_id,
                    text,
                    markup,
                    photo_file_id=photo_file_id,
                )
            except (TelegramForbiddenError, TelegramBadRequest) as exc:
                logger.warning("Тикет {} не доставлен менеджеру {}: {}", ticket.id, manager_id, exc)
                continue
            delivered = True
        return delivered

    async def _send_routed_message(
        self,
        session: AsyncSession,
        bot: Bot,
        ticket: Ticket,
        recipient_id: int,
        text: str,
        markup: InlineKeyboardMarkup | None = None,
        *,
        photo_file_id: str | None = None,
    ) -> None:
        """Сохранить маршрут каждой части; длинный текст фото не обрезается."""
        if photo_file_id is not None:
            short_caption = telegram_length(text) <= TELEGRAM_CAPTION_LIMIT
            sent_photo = await bot.send_photo(
                recipient_id,
                photo_file_id,
                caption=text if short_caption else None,
                reply_markup=markup if short_caption else None,
            )
            await routes_repo.remember(
                session,
                chat_id=sent_photo.chat.id,
                message_id=sent_photo.message_id,
                ticket_id=ticket.id,
                created_at=self._clock.now(),
            )
            if short_caption:
                return
        chunks = _split(text)
        for index, chunk in enumerate(chunks):
            sent = await bot.send_message(
                recipient_id, chunk, reply_markup=markup if index == len(chunks) - 1 else None
            )
            await routes_repo.remember(
                session,
                chat_id=sent.chat.id,
                message_id=sent.message_id,
                ticket_id=ticket.id,
                created_at=self._clock.now(),
            )

    async def _category_title(self, session: AsyncSession, key: str) -> str:
        category = await content_repo.category(session, key)
        return category.title if category is not None else category_title(self._config, key)

    async def _notify_client(
        self, session: AsyncSession, bot: Bot, ticket: Ticket, text: str
    ) -> None:
        try:
            sent = await bot.send_message(ticket.telegram_user_id, text)
        except (TelegramForbiddenError, TelegramBadRequest) as exc:
            logger.warning("Уведомление клиента по тикету {} не доставлено: {}", ticket.id, exc)
            return
        # Ответ клиента на это уведомление тоже должен найти тикет.
        await routes_repo.remember(
            session,
            chat_id=sent.chat.id,
            message_id=sent.message_id,
            ticket_id=ticket.id,
            created_at=self._clock.now(),
        )
