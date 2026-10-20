"""Модели SQLAlchemy — отражение схемы из миграций.

Миграции — источник правды, модели — для репозиториев.
NEVER call Base.metadata.create_all() — только миграции создают схему.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import BigInteger, Boolean, DateTime, Integer, MetaData, Text, Uuid, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

SCHEMA = "support"

# timestamptz (см. migrations/0001): в схеме все моменты времени — с поясом.
# Без явного типа Mapped[dt.datetime] даёт TIMESTAMP WITHOUT TIME ZONE, и
# aware-datetime из clock.now() отвергается asyncpg ещё до запроса.
TZ = DateTime(timezone=True)


class Base(DeclarativeBase):
    metadata = MetaData(schema=SCHEMA)


class Ticket(Base):
    __tablename__ = "tickets"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    guid: Mapped[uuid.UUID] = mapped_column(Uuid)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger)
    telegram_username: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text, server_default=text("'new'"))
    manager_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[dt.datetime] = mapped_column(TZ)
    assigned_at: Mapped[dt.datetime | None] = mapped_column(TZ)
    closed_at: Mapped[dt.datetime | None] = mapped_column(TZ)
    last_message_at: Mapped[dt.datetime] = mapped_column(TZ)


class StaffMember(Base):
    __tablename__ = "staff_members"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    telegram_user_id: Mapped[int | None] = mapped_column(BigInteger)
    telegram_username: Mapped[str] = mapped_column(Text)
    display_name: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[dt.datetime] = mapped_column(TZ)
    updated_at: Mapped[dt.datetime] = mapped_column(TZ)


class TicketAssignment(Base):
    __tablename__ = "ticket_assignments"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ticket_id: Mapped[int] = mapped_column(BigInteger)
    staff_telegram_user_id: Mapped[int] = mapped_column(BigInteger)
    assigned_by_telegram_user_id: Mapped[int | None] = mapped_column(BigInteger)
    assigned_at: Mapped[dt.datetime] = mapped_column(TZ)
    ended_at: Mapped[dt.datetime | None] = mapped_column(TZ)


class MessageHistory(Base):
    __tablename__ = "message_history"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ticket_id: Mapped[int] = mapped_column(BigInteger)
    from_user_id: Mapped[int] = mapped_column(BigInteger)
    message_text: Mapped[str] = mapped_column(Text)
    photo_file_id: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[dt.datetime] = mapped_column(TZ)


class MessageRoute(Base):
    """Сообщение бота в чате менеджеров → тикет (см. migrations/0002)."""

    __tablename__ = "message_routes"
    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    message_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ticket_id: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[dt.datetime] = mapped_column(TZ)


class Category(Base):
    __tablename__ = "categories"
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    title: Mapped[str] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean)
    sort_order: Mapped[int] = mapped_column(Integer)


class InstructionSection(Base):
    __tablename__ = "instruction_sections"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    title: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)
    sort_order: Mapped[int] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(Boolean)


class Product(Base):
    __tablename__ = "products"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    sku: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text)
    pdf_file_id: Mapped[str] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(TZ)
    updated_at: Mapped[dt.datetime] = mapped_column(TZ)


class ProductImage(Base):
    __tablename__ = "product_images"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    product_id: Mapped[int] = mapped_column(BigInteger)
    file_id: Mapped[str] = mapped_column(Text)
    sort_order: Mapped[int] = mapped_column(Integer)


class ChatUiMessage(Base):
    __tablename__ = "chat_ui_messages"
    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    message_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_at: Mapped[dt.datetime] = mapped_column(TZ)


class FaqType(Base):
    __tablename__ = "faq_types"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    title: Mapped[str] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean)
    sort_order: Mapped[int] = mapped_column(Integer)


class FaqItem(Base):
    __tablename__ = "faq_items"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    type_id: Mapped[int | None] = mapped_column(BigInteger)
    question: Mapped[str] = mapped_column(Text)
    answer: Mapped[str] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean)
    sort_order: Mapped[int] = mapped_column(Integer)
