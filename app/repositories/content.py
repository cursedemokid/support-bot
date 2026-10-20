"""Каталог, инструкции и категории: только SQL, без Telegram-логики."""

from __future__ import annotations

import datetime as dt
from typing import Literal

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Category, InstructionSection, Product, ProductImage


async def categories(session: AsyncSession) -> list[Category]:
    result = await session.execute(
        select(Category).where(Category.is_active).order_by(Category.sort_order, Category.title)
    )
    return list(result.scalars())


async def category(session: AsyncSession, key: str) -> Category | None:
    found = await session.get(Category, key)
    return found if found is not None and found.is_active else None


async def sections(session: AsyncSession) -> list[InstructionSection]:
    result = await session.execute(
        select(InstructionSection)
        .where(InstructionSection.is_active)
        .order_by(InstructionSection.sort_order, InstructionSection.title)
    )
    return list(result.scalars())


async def section(session: AsyncSession, section_id: int) -> InstructionSection | None:
    found = await session.get(InstructionSection, section_id)
    return found if found is not None and found.is_active else None


async def find_products(session: AsyncSession, query: str, limit: int = 10) -> list[Product]:
    pattern = f"%{query.strip()}%"
    result = await session.execute(
        select(Product)
        .where(or_(Product.sku.ilike(pattern), Product.title.ilike(pattern)))
        .order_by(Product.title)
        .limit(limit)
    )
    return list(result.scalars())


async def product(session: AsyncSession, product_id: int) -> Product | None:
    return await session.get(Product, product_id)


async def product_images(session: AsyncSession, product_id: int) -> list[ProductImage]:
    result = await session.execute(
        select(ProductImage)
        .where(ProductImage.product_id == product_id)
        .order_by(ProductImage.sort_order, ProductImage.id)
    )
    return list(result.scalars())


async def create_product(
    session: AsyncSession,
    *,
    sku: str,
    title: str,
    description: str,
    pdf_file_id: str,
    image_file_ids: list[str] | None = None,
    now: dt.datetime,
) -> Product:
    item = Product(
        sku=sku,
        title=title,
        description=description,
        pdf_file_id=pdf_file_id,
        created_at=now,
        updated_at=now,
    )
    session.add(item)
    await session.flush()
    for sort_order, file_id in enumerate((image_file_ids or [])[:3]):
        session.add(ProductImage(product_id=item.id, file_id=file_id, sort_order=sort_order))
    await session.flush()
    return item


async def update_product(
    session: AsyncSession,
    *,
    product_id: int,
    sku: str,
    title: str,
    description: str,
    pdf_file_id: str,
    image_file_ids: list[str],
    now: dt.datetime,
) -> Product | None:
    item = await session.get(Product, product_id)
    if item is None:
        return None
    item.sku = sku
    item.title = title
    item.description = description
    item.pdf_file_id = pdf_file_id
    item.updated_at = now
    existing = await product_images(session, product_id)
    for image in existing:
        await session.delete(image)
    for sort_order, file_id in enumerate(image_file_ids[:3]):
        session.add(ProductImage(product_id=item.id, file_id=file_id, sort_order=sort_order))
    await session.flush()
    return item


async def update_product_field(
    session: AsyncSession,
    *,
    product_id: int,
    field: Literal["sku", "title", "description", "pdf_file_id"],
    value: str,
    now: dt.datetime,
) -> Product | None:
    item = await session.get(Product, product_id)
    if item is None:
        return None
    setattr(item, field, value)
    item.updated_at = now
    await session.flush()
    return item


async def replace_product_images(
    session: AsyncSession,
    *,
    product_id: int,
    image_file_ids: list[str],
    now: dt.datetime,
) -> Product | None:
    item = await session.get(Product, product_id)
    if item is None:
        return None
    existing = await product_images(session, product_id)
    for image in existing:
        await session.delete(image)
    for sort_order, file_id in enumerate(image_file_ids[:3]):
        session.add(ProductImage(product_id=product_id, file_id=file_id, sort_order=sort_order))
    item.updated_at = now
    await session.flush()
    return item


async def delete_product(session: AsyncSession, product_id: int) -> bool:
    item = await session.get(Product, product_id)
    if item is None:
        return False
    await session.delete(item)
    await session.flush()
    return True


async def create_section(session: AsyncSession, *, title: str, body: str) -> InstructionSection:
    item = InstructionSection(title=title, body=body, sort_order=0, is_active=True)
    session.add(item)
    await session.flush()
    return item


async def create_category(session: AsyncSession, *, key: str, title: str) -> Category:
    item = Category(key=key, title=title, sort_order=0, is_active=True)
    session.add(item)
    await session.flush()
    return item
