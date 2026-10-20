"""Клавиатуры бота. Собираются из конфига, callback_data — из app/bot/callbacks.py."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.bot.callbacks import category_cb, close_cb, take_cb
from app.db.models import Product


class _Category(Protocol):
    key: str
    title: str


def categories_keyboard(
    categories: Iterable[_Category], *, back_data: str | None = None
) -> InlineKeyboardMarkup:
    """Категории обращения: по одной кнопке в строке.

    По одной, а не в два столбца: заголовки категорий — «Как пользоваться»,
    «Оплата и возврат» — в две колонки обрезаются на узких экранах.
    """
    builder = InlineKeyboardBuilder()
    for category in categories:
        builder.button(text=category.title, callback_data=category_cb(category.key))
    if back_data is not None:
        builder.button(text="⬅️ Назад", callback_data=back_data)
    builder.adjust(1)
    return builder.as_markup()


def ticket_keyboard(ticket_id: int, *, taken: bool) -> InlineKeyboardMarkup:
    """Кнопки под карточкой тикета: «Взять» и «Закрыть».

    «Взять» показывается только у невзятого тикета: у взятого она уже
    ничего не значит, а место занимает.
    """
    builder = InlineKeyboardBuilder()
    if not taken:
        builder.button(text="🙋 Взять в работу", callback_data=take_cb(ticket_id))
    builder.button(text="✍️ Ответить", callback_data=f"mgrreply:{ticket_id}")
    builder.button(text="✅ Закрыть", callback_data=close_cb(ticket_id))
    builder.adjust(1)
    return builder.as_markup()


def manager_notification_keyboard(ticket_id: int) -> InlineKeyboardMarkup:
    """Действие под сообщением клиента в личном чате сотрудника."""
    builder = InlineKeyboardBuilder()
    builder.button(text="✍️ Ответить", callback_data=f"mgrreply:{ticket_id}")
    return builder.as_markup()


def queue_keyboard(tickets: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    """Список открытых тикетов: (id, подпись) → кнопки «Взять»."""
    builder = InlineKeyboardBuilder()
    for ticket_id, label in tickets:
        builder.button(text=label, callback_data=take_cb(ticket_id))
    builder.adjust(1)
    return builder.as_markup()


def client_menu() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="📚 Инструкции к товарам", callback_data="nav:catalog")
    builder.button(text="🛠 Техническая поддержка", callback_data="nav:support")
    builder.adjust(1)
    return builder.as_markup()


def manager_menu(*, admin: bool) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="📬 Открытые обращения", callback_data="mgr:open")
    builder.button(text="🟡 В работе", callback_data="mgr:assigned")
    builder.button(text="🗂 История", callback_data="mgr:history")
    builder.button(text="📚 Каталог товаров", callback_data="mgr:catalog")
    if admin:
        builder.button(text="⚙️ Управление контентом", callback_data="adm:menu")
        builder.button(text="👥 Сотрудники", callback_data="adm:staff")
    builder.adjust(1)
    return builder.as_markup()


def list_keyboard(prefix: str, rows: list[tuple[int | str, str]]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for value, title in rows:
        builder.button(text=title, callback_data=f"{prefix}:{value}")
    builder.adjust(1)
    return builder.as_markup()


def with_back(
    prefix: str, rows: list[tuple[int | str, str]], back_data: str, back_text: str = "⬅️ Назад"
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for value, title in rows:
        builder.button(text=title, callback_data=f"{prefix}:{value}")
    builder.button(text=back_text, callback_data=back_data)
    builder.adjust(1)
    return builder.as_markup()


def back_keyboard(back_data: str, back_text: str = "⬅️ Назад") -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=back_text, callback_data=back_data)
    return builder.as_markup()


def support_menu() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="❓ Вопросы и ответы", callback_data="sup:faq")
    builder.button(text="💬 Мои обращения", callback_data="sup:tickets")
    builder.button(text="➕ Создать обращение", callback_data="sup:create")
    builder.button(text="⬅️ Назад", callback_data="menu:root")
    builder.adjust(1)
    return builder.as_markup()


def instructions_menu(products: Iterable[Product]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🔎 Найти товар", callback_data="nav:product-search")
    for product in products:
        builder.button(text=f"{product.sku} · {product.title}", callback_data=f"prod:{product.id}")
    builder.button(text="⬅️ Назад", callback_data="menu:root")
    builder.adjust(1)
    return builder.as_markup()


def chat_keyboard(
    *, writable: bool, back_data: str, take_ticket_id: int | None = None
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if take_ticket_id is not None:
        builder.button(text="🙋 Взять в работу", callback_data=f"chat:take:{take_ticket_id}")
    if writable:
        builder.button(text="✍️ Написать", callback_data="chat:write")
    builder.button(text="⬅️ Назад", callback_data=back_data)
    builder.adjust(1)
    return builder.as_markup()


def manager_chat_keyboard(
    *,
    writable: bool,
    take_ticket_id: int | None = None,
    transfer_ticket_id: int | None = None,
    close_ticket_id: int | None = None,
    back_data: str = "mgr:list",
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if take_ticket_id is not None:
        builder.button(text="🙋 Взять в работу", callback_data=f"mgrchat:take:{take_ticket_id}")
    if writable:
        builder.button(text="✍️ Написать", callback_data="mgrchat:write")
    if transfer_ticket_id is not None:
        builder.button(text="↪️ Передать", callback_data=f"mgrchat:transfer:{transfer_ticket_id}")
    if close_ticket_id is not None:
        builder.button(text="✅ Закрыть", callback_data=f"mgrchat:close:{close_ticket_id}")
    builder.button(text="⬅️ Назад", callback_data=back_data)
    builder.adjust(1)
    return builder.as_markup()


def product_keyboard(product: Product, *, back_data: str = "nav:catalog") -> InlineKeyboardMarkup:
    return with_back("pdf", [(product.id, "📄 Скачать PDF-инструкцию")], back_data)


def admin_product_keyboard(product_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✏️ Артикул", callback_data=f"admprodedit:sku:{product_id}")
    builder.button(text="✏️ Название", callback_data=f"admprodedit:title:{product_id}")
    builder.button(text="✏️ Описание", callback_data=f"admprodedit:description:{product_id}")
    builder.button(text="🖼 Фотографии", callback_data=f"admprodedit:photos:{product_id}")
    builder.button(text="📄 PDF-инструкция", callback_data=f"admprodedit:pdf:{product_id}")
    builder.button(text="🗑 Удалить", callback_data=f"admprodact:delete:{product_id}")
    builder.button(text="⬅️ Назад", callback_data="adm:products")
    builder.adjust(1)
    return builder.as_markup()


def admin_menu() -> InlineKeyboardMarkup:
    return with_back(
        "adm",
        [
            ("products", "Товары"),
            ("categories", "Категории"),
            ("faq", "Вопросы и ответы"),
        ],
        "mgr:home",
    )


def faq_admin_menu() -> InlineKeyboardMarkup:
    return with_back(
        "admfaq",
        [
            ("types", "Типы"),
            ("questions", "Вопросы"),
            ("download", "Скачать Excel"),
            ("upload", "Загрузить Excel"),
        ],
        "adm:menu",
    )


def faq_upload_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Скачать свежий шаблон", callback_data="admfaq:download")
    builder.button(text="⬅️ Назад", callback_data="adm:faq")
    builder.adjust(1)
    return builder.as_markup()


def faq_types_keyboard(rows: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    values: list[tuple[int | str, str]] = [("add", "Добавить тип")]
    values.extend(rows)
    return with_back("admfaqtype", values, "adm:faq")


def faq_type_card_keyboard(type_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Изменить порядок", callback_data=f"admfaqtypeorder:{type_id}")
    builder.button(text="Удалить тип", callback_data=f"admfaqtypedel:{type_id}")
    builder.button(text="⬅️ Назад", callback_data="admfaq:types")
    builder.adjust(1)
    return builder.as_markup()


def faq_question_types_keyboard(
    rows: list[tuple[int, str]], *, has_orphans: bool
) -> InlineKeyboardMarkup:
    values: list[tuple[int | str, str]] = []
    if has_orphans:
        values.append(("orphan", "Без типа"))
    values.extend(rows)
    return with_back("admfaqquestions", values, "adm:faq")


def faq_questions_keyboard(
    rows: list[tuple[int, str]], *, type_id: int | None
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if type_id is not None:
        builder.button(text="Добавить вопрос", callback_data=f"admfaqadd:{type_id}")
    for item_id, title in rows:
        builder.button(text=title, callback_data=f"admfaqitem:{item_id}")
    builder.button(text="⬅️ Назад", callback_data="admfaq:questions")
    builder.adjust(1)
    return builder.as_markup()


def faq_item_card_keyboard(item_id: int, *, type_id: int | None) -> InlineKeyboardMarkup:
    parent = "orphan" if type_id is None else str(type_id)
    builder = InlineKeyboardBuilder()
    builder.button(text="Перенести в тип", callback_data=f"admfaqitemmove:{item_id}")
    builder.button(text="Удалить", callback_data=f"admfaqitemdel:{item_id}")
    builder.button(text="⬅️ Назад", callback_data=f"admfaqquestions:{parent}")
    builder.adjust(1)
    return builder.as_markup()


def faq_item_move_keyboard(item_id: int, rows: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for type_id, title in rows:
        builder.button(text=title, callback_data=f"admfaqitemassign:{item_id}:{type_id}")
    builder.button(text="⬅️ Назад", callback_data=f"admfaqitem:{item_id}")
    builder.adjust(1)
    return builder.as_markup()
