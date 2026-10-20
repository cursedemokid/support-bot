"""Схема на живой базе: миграции, изоляция в support, соответствие моделей.

Требует TEST_DATABASE_URL (см. conftest), без него пропускается.
"""

from __future__ import annotations

from pathlib import Path

import asyncpg
import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from app.db.models import SCHEMA, Base, FaqItem
from app.migrate import MigrationError, migrate

REPO_ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS = REPO_ROOT / "migrations"

EXPECTED_TABLES = {
    "categories",
    "instruction_sections",
    "products",
    "product_images",
    "chat_ui_messages",
    "faq_types",
    "faq_items",
    "tickets",
    "message_history",
    "message_routes",
    "staff_members",
    "ticket_assignments",
}


async def _tables(conn: asyncpg.Connection) -> set[str]:
    rows = await conn.fetch(
        "select table_name from information_schema.tables where table_schema = $1",
        SCHEMA,
    )
    return {str(row["table_name"]) for row in rows} - {"schema_migrations"}


async def test_migrations_create_expected_tables(migrated_db: str) -> None:
    conn = await asyncpg.connect(migrated_db.replace("postgresql+asyncpg://", "postgresql://"))
    try:
        assert await _tables(conn) == EXPECTED_TABLES
        schemas = await conn.fetch(
            "select schema_name from information_schema.schemata where schema_name = $1", SCHEMA
        )
        assert len(schemas) == 1
    finally:
        await conn.close()


async def test_migrations_are_idempotent(migrated_db: str) -> None:
    """Повторный прогон ничего не применяет: в этом и смысл forward-only раннера."""
    assert await migrate(migrated_db, MIGRATIONS) == []


async def test_public_schema_untouched(migrated_db: str) -> None:
    """Схема support — единственное, что создаёт бот: public общий с соседями."""
    conn = await asyncpg.connect(migrated_db.replace("postgresql+asyncpg://", "postgresql://"))
    try:
        rows = await conn.fetch(
            "select table_name from information_schema.tables where table_schema = 'public'"
        )
        assert rows == []
    finally:
        await conn.close()


async def test_models_match_database(migrated_db: str) -> None:
    """Колонки моделей совпадают со схемой, созданной миграциями.

    Модели и SQL пишутся раздельно — без этой проверки расхождение (переименовали
    колонку в миграции, забыли в модели) всплыло бы только на живом запросе.
    """
    conn = await asyncpg.connect(migrated_db.replace("postgresql+asyncpg://", "postgresql://"))
    try:
        for table in Base.metadata.sorted_tables:
            rows = await conn.fetch(
                "select column_name from information_schema.columns "
                "where table_schema = $1 and table_name = $2",
                SCHEMA,
                table.name,
            )
            in_db = {str(row["column_name"]) for row in rows}
            in_model = {col.name for col in table.columns}
            assert in_model == in_db, f"{table.name}: модель {in_model} != база {in_db}"
    finally:
        await conn.close()


async def test_migrations_seed_only_production_catalog_and_empty_faq(
    migrated_db: str,
) -> None:
    conn = await asyncpg.connect(migrated_db.replace("postgresql+asyncpg://", "postgresql://"))
    try:
        products = await conn.fetch(
            "select sku, title, description, pdf_file_id "
            "from support.products order by sku"
        )
        assert [
            (row["sku"], row["title"], row["pdf_file_id"])
            for row in products
        ] == [
            (
                "M815R45",
                "Паровая швабра DEMO BRAND M815R45",
                "asset://instructions/demo-brand-m815r45.pdf",
            ),
            (
                "W29A56",
                "Робот для мытья окон DEMO BRAND W29A56",
                "asset://instructions/demo-brand-w29a56.pdf",
            ),
        ]
        assert all("Характеристики" in str(row["description"]) for row in products)

        images = await conn.fetch(
            "select p.sku, i.file_id, i.sort_order "
            "from support.product_images i "
            "join support.products p on p.id = i.product_id "
            "order by p.sku, i.sort_order"
        )
        assert [(row["sku"], row["file_id"], row["sort_order"]) for row in images] == [
            ("M815R45", "asset://products/demo-brand-m815r45.png", 0),
            ("W29A56", "asset://products/demo-brand-w29a56.png", 0),
        ]

        assert await conn.fetchval("select count(*) from support.faq_items") == 0
        assert await conn.fetchval("select count(*) from support.faq_types") == 0
    finally:
        await conn.close()


def test_model_ddl_targets_support_schema() -> None:
    """Ни одна таблица модели не метит в public: проверка без базы."""
    for table in Base.metadata.sorted_tables:
        ddl = str(CreateTable(table).compile(dialect=postgresql.dialect()))
        assert f"{SCHEMA}.{table.name}" in ddl


def test_faq_item_type_is_nullable_and_migration_preserves_orphans() -> None:
    assert FaqItem.__table__.c.type_id.nullable

    migration = (MIGRATIONS / "0008_faq_orphans.sql").read_text(encoding="utf-8").lower()
    assert "alter column type_id drop not null" in migration
    assert "on delete set null" in migration


async def test_checksum_drift_is_detected(migrated_db: str, tmp_path: Path) -> None:
    """Правка уже применённой миграции — ошибка, а не молчаливый пропуск."""
    target = tmp_path / "migrations"
    target.mkdir()
    for src in MIGRATIONS.glob("*.sql"):
        target.joinpath(src.name).write_text(src.read_text(encoding="utf-8"))

    first = sorted(target.glob("*.sql"))[0]
    first.write_text(first.read_text(encoding="utf-8") + "\n-- правка после применения\n")

    with pytest.raises(MigrationError):
        await migrate(migrated_db, target)
