"""Представление карточки товара в клиентском каталоге."""

from __future__ import annotations

from collections.abc import Sequence

from aiogram import html

from app.db.models import Product
from app.services.ui import UiPhoto

MAX_PRODUCT_IMAGES = 3


def normalize_product_image_ids(file_ids: Sequence[str]) -> tuple[str, ...]:
    """Проверить ограничение галереи, не допуская пустых карточек."""

    values = tuple(file_id.strip() for file_id in file_ids if file_id.strip())
    if not 1 <= len(values) <= MAX_PRODUCT_IMAGES:
        msg = "У товара должно быть от 1 до 3 изображений."
        raise ValueError(msg)
    return values


def product_card_parts(product: Product, image_file_ids: Sequence[str]) -> list[str | UiPhoto]:
    """Собрать галерею, затем текстовую часть карточки товара.

    Текст идёт отдельным сообщением: подпись к фотографии ограничена Telegram
    1024 символами, тогда как описание товара может быть заметно длиннее.
    """

    photos = [
        UiPhoto(file_id=file_id, caption="") for file_id in image_file_ids[:MAX_PRODUCT_IMAGES]
    ]
    details = (
        f"<b>{html.quote(product.title)}</b>\n"
        f"Артикул: <code>{html.quote(product.sku)}</code>\n\n"
        f"{html.quote(product.description)}"
    )
    return [*photos, details]
