from __future__ import annotations

import datetime as dt

import pytest

from app.bot import keyboards
from app.bot.keyboards import (
    admin_menu,
    instructions_menu,
    manager_notification_keyboard,
    product_keyboard,
)
from app.catalog import normalize_product_image_ids, product_card_parts
from app.db.models import Product
from app.services.ui import UiPhoto


def test_product_card_shows_at_most_three_photos_before_product_details() -> None:
    product = Product(
        id=1,
        sku="QW-DEMO-01",
        title="DEMO BRAND Demo",
        description="Описание демонстрационного товара.",
        pdf_file_id="instruction.pdf",
        created_at=dt.datetime.now(tz=dt.UTC),
        updated_at=dt.datetime.now(tz=dt.UTC),
    )

    parts = product_card_parts(product, ["photo-1", "photo-2", "photo-3", "photo-4"])

    assert [part.file_id for part in parts[:-1] if isinstance(part, UiPhoto)] == [
        "photo-1",
        "photo-2",
        "photo-3",
    ]
    assert parts[-1] == (
        "<b>DEMO BRAND Demo</b>\nАртикул: <code>QW-DEMO-01</code>\n\nОписание демонстрационного товара."
    )


def test_product_gallery_requires_between_one_and_three_photos() -> None:
    assert normalize_product_image_ids(["photo-1", "photo-2"]) == ("photo-1", "photo-2")

    with pytest.raises(ValueError, match="от 1 до 3"):
        normalize_product_image_ids([])
    with pytest.raises(ValueError, match="от 1 до 3"):
        normalize_product_image_ids(["1", "2", "3", "4"])


def test_instructions_menu_lists_products_between_search_and_back() -> None:
    products = [
        Product(id=7, sku="QW-V16-01", title="DEMO BRAND V16 Flex"),
        Product(id=8, sku="QW-SG01", title="DEMO BRAND SteamGo S1"),
    ]

    markup = instructions_menu(products)

    buttons = [button for row in markup.inline_keyboard for button in row]
    assert [(button.text, button.callback_data) for button in buttons] == [
        ("🔎 Найти товар", "nav:product-search"),
        ("QW-V16-01 · DEMO BRAND V16 Flex", "prod:7"),
        ("QW-SG01 · DEMO BRAND SteamGo S1", "prod:8"),
        ("⬅️ Назад", "menu:root"),
    ]


def test_employee_product_back_returns_to_employee_catalog() -> None:
    product = Product(id=7, sku="QW-V16-01", title="DEMO BRAND V16 Flex")

    markup = product_keyboard(product, back_data="mgr:catalog")

    buttons = [button for row in markup.inline_keyboard for button in row]
    assert [(button.text, button.callback_data) for button in buttons] == [
        ("📄 Скачать PDF-инструкцию", "pdf:7"),
        ("⬅️ Назад", "mgr:catalog"),
    ]


def test_ticket_notification_offers_reply_through_bot() -> None:
    markup = manager_notification_keyboard(23)

    buttons = [button for row in markup.inline_keyboard for button in row]
    assert [(button.text, button.callback_data) for button in buttons] == [
        ("✍️ Ответить", "mgrreply:23"),
    ]


def test_admin_menu_has_back_to_employee_cabinet() -> None:
    markup = admin_menu()

    buttons = [button for row in markup.inline_keyboard for button in row]
    assert (buttons[-1].text, buttons[-1].callback_data) == ("⬅️ Назад", "mgr:home")


def test_admin_product_menu_edits_each_field_separately() -> None:
    """Catches a product card collapsing all edits into one long wizard."""
    factory = getattr(keyboards, "admin_product_keyboard", None)
    assert factory is not None, "admin product keyboard is missing"

    markup = factory(7)

    buttons = [button for row in markup.inline_keyboard for button in row]
    assert [(button.text, button.callback_data) for button in buttons] == [
        ("✏️ Артикул", "admprodedit:sku:7"),
        ("✏️ Название", "admprodedit:title:7"),
        ("✏️ Описание", "admprodedit:description:7"),
        ("🖼 Фотографии", "admprodedit:photos:7"),
        ("📄 PDF-инструкция", "admprodedit:pdf:7"),
        ("🗑 Удалить", "admprodact:delete:7"),
        ("⬅️ Назад", "adm:products"),
    ]
