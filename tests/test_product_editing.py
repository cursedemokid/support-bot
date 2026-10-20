from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, User

from app.bot.handlers import admin_content
from app.db.models import Product, ProductImage
from app.repositories import content as content_repo
from tests.factories import FakeBot, private_message


class _Scalars:
    def __init__(self, values: list[ProductImage]) -> None:
        self._values = values

    def __iter__(self):
        return iter(self._values)


class _Result:
    def __init__(self, values: list[ProductImage]) -> None:
        self._values = values

    def scalars(self) -> _Scalars:
        return _Scalars(self._values)


class _Session:
    def __init__(self, product: Product, images: list[ProductImage] | None = None) -> None:
        self.product = product
        self.images = images or []
        self.added: list[Any] = []
        self.deleted: list[Any] = []
        self.flushed = False

    async def get(self, model, item_id: int):
        assert model is Product
        return self.product if item_id == self.product.id else None

    async def execute(self, statement) -> _Result:
        return _Result(self.images)

    def add(self, item: Any) -> None:
        self.added.append(item)

    async def delete(self, item: Any) -> None:
        self.deleted.append(item)

    async def flush(self) -> None:
        self.flushed = True

    async def commit(self) -> None:
        pass


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("sku", "QW-NEW", ("QW-NEW", "Old title", "Old description", "old.pdf")),
        ("title", "New title", ("QW-OLD", "New title", "Old description", "old.pdf")),
        (
            "description",
            "New description",
            ("QW-OLD", "Old title", "New description", "old.pdf"),
        ),
        ("pdf_file_id", "new.pdf", ("QW-OLD", "Old title", "Old description", "new.pdf")),
    ],
)
async def test_update_product_field_changes_only_selected_value(
    field: str, value: str, expected: tuple[str, str, str, str]
) -> None:
    """Catches a single-field edit overwriting unrelated product data."""
    updater = getattr(content_repo, "update_product_field", None)
    assert updater is not None, "single-field product updater is missing"
    product = Product(
        id=7,
        sku="QW-OLD",
        title="Old title",
        description="Old description",
        pdf_file_id="old.pdf",
    )
    session = _Session(product)
    now = dt.datetime(2026, 10, 8, tzinfo=dt.UTC)

    updated = await updater(
        session,
        product_id=7,
        field=field,
        value=value,
        now=now,
    )

    assert updated is product
    assert (product.sku, product.title, product.description, product.pdf_file_id) == expected
    assert product.updated_at == now
    assert session.flushed


async def test_replace_product_images_replaces_the_complete_gallery() -> None:
    """Catches new product photos being appended to the old gallery."""
    replace = getattr(content_repo, "replace_product_images", None)
    assert replace is not None, "product image replacement is missing"
    product = Product(id=7, sku="QW", title="Product", description="D", pdf_file_id="old.pdf")
    old_images = [
        ProductImage(id=1, product_id=7, file_id="old-1", sort_order=0),
        ProductImage(id=2, product_id=7, file_id="old-2", sort_order=1),
    ]
    session = _Session(product, old_images)
    now = dt.datetime(2026, 10, 8, tzinfo=dt.UTC)

    updated = await replace(
        session,
        product_id=7,
        image_file_ids=["new-1", "new-2"],
        now=now,
    )

    assert updated is product
    assert session.deleted == old_images
    assert [(image.file_id, image.sort_order) for image in session.added] == [
        ("new-1", 0),
        ("new-2", 1),
    ]
    assert product.updated_at == now
    assert session.flushed


async def test_returning_to_product_card_cancels_field_edit(clock) -> None:
    """Catches Back leaving the administrator stuck in an edit FSM state."""
    product = Product(id=7, sku="QW", title="Product", description="D", pdf_file_id="old.pdf")
    session = _Session(product)
    bot = FakeBot()
    state = FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=1, chat_id=1, user_id=1),
    )
    await state.set_state(admin_content.AdminFlow.product_title)
    await state.update_data(editing_product_id=7, editing_product_field="title")
    query = CallbackQuery(
        id="callback-id",
        from_user=User(id=1, is_bot=False, first_name="Admin"),
        chat_instance="chat-instance",
        data="admprod:7",
    ).as_(bot)  # type: ignore[arg-type]

    await admin_content.product_actions(  # type: ignore[arg-type]
        query,
        session=session,
        bot=bot,
        clock=clock,
        state=state,
    )

    assert await state.get_state() is None


async def test_single_field_save_returns_directly_to_product_card(clock) -> None:
    """Catches a successful edit adding an extra confirmation screen."""
    product = Product(id=7, sku="QW", title="Old title", description="D", pdf_file_id="old.pdf")
    session = _Session(product)
    bot = FakeBot()
    state = FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=1, chat_id=1, user_id=1),
    )
    await state.set_state(admin_content.AdminFlow.product_title)
    await state.update_data(editing_product_id=7, editing_product_field="title")
    message = private_message("New title", user_id=1).as_(bot)  # type: ignore[arg-type]

    await admin_content.product_title(  # type: ignore[arg-type]
        message,
        state=state,
        session=session,
        bot=bot,
        clock=clock,
    )

    assert bot.texts[-1] == "<b>New title</b>\nАртикул: <code>QW</code>"
    assert await state.get_state() is None
