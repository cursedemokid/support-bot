"""Грамматика callback_data: что собрали, то и разобрали; мусор — None.

Формат кнопки и её разбор живут в одном файле (app/bot/callbacks.py), и эти
тесты держат обе половины вместе: добавленная кнопка без разбора падает здесь,
а не тихим «нажатие ничего не делает» в бою.
"""

from __future__ import annotations

import pytest
from aiogram.types import CallbackQuery, User

from app.bot import callbacks
from app.bot.handlers import admin_content, manager_commands
from app.bot.keyboards import admin_menu


def _callback_data(markup) -> list[str]:
    return [button.callback_data or "" for row in markup.inline_keyboard for button in row]


def test_admin_menu_does_not_offer_legacy_instructions() -> None:
    assert "adm:instructions" not in _callback_data(admin_menu())


@pytest.mark.parametrize("key", ["defect", "usage", "payment", "other", "a", "x_1"])
def test_category_round_trip(key: str) -> None:
    parsed = callbacks.parse(callbacks.category_cb(key))
    assert parsed == callbacks.PickCategory(key)


def test_take_round_trip() -> None:
    assert callbacks.parse(callbacks.take_cb(42)) == callbacks.TakeTicket(42)


def test_close_round_trip() -> None:
    assert callbacks.parse(callbacks.close_cb(7)) == callbacks.CloseTicket(7)


@pytest.mark.parametrize(
    "data",
    [
        "",
        "cat",
        "cat:",
        "cat:ЗЛОЙ",
        "cat:DEFECT",
        "cat:" + "a" * 33,
        "tk",
        "tk:take",
        "tk:take:0",
        "tk:take:-5",
        "tk:take:abc",
        "tk:drop:1",
        "other:1",
        "cat:defect:extra",
    ],
)
def test_junk_is_rejected(data: str) -> None:
    """Не наше, подделка, опечатка — один ответ None, без исключений."""
    assert callbacks.parse(data) is None


@pytest.mark.parametrize(
    ("handler", "data"),
    [
        (admin_content.staff_details, "admstaff:7"),
        (admin_content.staff_edit_name_start, "admstaffedit:name:7"),
        (admin_content.staff_update, "admstaffedit:role:7:support"),
        (admin_content.product_actions, "admprod:7"),
        (admin_content.edit_product_start, "admprodact:edit:7"),
        (admin_content.delete_product_confirm, "admprodact:delete:7"),
        (admin_content.delete_product_done, "admproddel:7"),
        (manager_commands.transfer_ticket, "mgrxfer:7:8"),
    ],
)
def test_numeric_callback_reaches_its_handler(handler, data: str) -> None:
    """A callback emitted by a numeric list button must match its handler."""
    query = CallbackQuery(
        id="callback-id",
        from_user=User(id=1, is_bot=False, first_name="Admin"),
        chat_instance="chat-instance",
        data=data,
    )
    handler_object = next(
        item
        for router in (admin_content.router, manager_commands.router)
        for item in router.callback_query.handlers
        if item.callback is handler
    )

    matched = all(
        item.magic is not None and bool(item.magic.resolve(query))
        for item in handler_object.filters
    )

    assert matched, f"callback {data!r} is not routed to {handler.__name__}"


@pytest.mark.parametrize("field", ["sku", "title", "description", "photos", "pdf"])
def test_product_field_callback_reaches_edit_handler(field: str) -> None:
    """Catches a field-edit button with no matching callback handler."""
    handler = getattr(admin_content, "edit_product_field_start", None)
    assert handler is not None, "product field edit handler is missing"
    query = CallbackQuery(
        id="callback-id",
        from_user=User(id=1, is_bot=False, first_name="Admin"),
        chat_instance="chat-instance",
        data=f"admprodedit:{field}:7",
    )
    handler_object = next(
        item for item in admin_content.router.callback_query.handlers if item.callback is handler
    )

    matched = all(
        item.magic is not None and bool(item.magic.resolve(query))
        for item in handler_object.filters
    )

    assert matched, f"field {field!r} is not routed to the edit handler"
