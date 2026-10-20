"""Общие фикстуры тестов.

Интеграционные тесты требуют TEST_DATABASE_URL и пропускаются без него.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path

import asyncpg
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.clock import ManualClock
from app.config import AppConfig, load_secrets, load_settings
from app.db.engine import build_engine
from app.db.session import build_session_factory
from app.migrate import migrate, to_asyncpg_dsn

REPO_ROOT = Path(__file__).resolve().parent.parent
REAL_SETTINGS = REPO_ROOT / "config" / "settings.yaml"
REFERENCE_SETTINGS = REPO_ROOT / "tests" / "fixtures" / "settings.yaml"


@pytest.fixture
def base_settings() -> dict:
    """Эталонные значения словарём — отправная точка для мутаций."""
    import yaml

    return yaml.safe_load(REFERENCE_SETTINGS.read_text(encoding="utf-8"))


@pytest.fixture
def write_settings(tmp_path: Path):
    import yaml

    def _write(data: object) -> Path:
        path = tmp_path / "settings.yaml"
        path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
        return path

    return _write


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Секреты из окружения убраны, чтобы тест не зависел от машины."""
    for name in ("BOT_TOKEN", "DATABASE_URL", "SENTRY_DSN", "LOG_LEVEL"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.fixture
def test_database_url() -> str:
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL не задан — интеграционные тесты пропущены")
    return url


@pytest.fixture
async def clean_db(test_database_url: str) -> AsyncIterator[asyncpg.Connection]:
    """Схема support снесена, public не тронута.

    Схему не восстанавливаем: следующий тест всё равно снесёт, зато после
    падения в базу можно заглянуть руками.
    """
    conn = await asyncpg.connect(to_asyncpg_dsn(test_database_url))
    try:
        await conn.execute("drop schema if exists support cascade")
        yield conn
    finally:
        await conn.close()


@pytest.fixture
async def migrated_db(clean_db: asyncpg.Connection, test_database_url: str) -> str:
    """Чистая база с применёнными миграциями."""
    await migrate(test_database_url, REPO_ROOT / "migrations")
    return test_database_url


@pytest.fixture
async def session_factory(
    migrated_db: str,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = build_engine(migrated_db)
    try:
        yield build_session_factory(engine)
    finally:
        await engine.dispose()


@pytest.fixture
def config(base_settings: dict, clean_env: pytest.MonkeyPatch, write_settings) -> AppConfig:
    """Боевой settings.yaml + подставные секреты.

    `path` — копия эталона в tmp: тест перезагрузки не должен трогать
    боевой файл.
    """
    settings = load_settings(REAL_SETTINGS)
    return _app_config(settings, clean_env, write_settings(base_settings))


@pytest.fixture
def app_config(base_settings: dict, clean_env: pytest.MonkeyPatch, write_settings) -> AppConfig:
    """Эталон настроек с тестовыми bootstrap-сотрудниками."""
    settings = load_settings(REFERENCE_SETTINGS)
    return _app_config(settings, clean_env, write_settings(base_settings))


def _app_config(settings: object, clean_env: pytest.MonkeyPatch, path: Path) -> AppConfig:
    clean_env.setenv("BOT_TOKEN", "123:TEST")
    clean_env.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@127.0.0.1:5432/db")
    return AppConfig(settings, load_secrets(), path=path)  # type: ignore[arg-type]


@pytest.fixture
def clock() -> ManualClock:
    import datetime as dt
    import zoneinfo

    return ManualClock(
        dt.datetime(2026, 5, 1, 12, 0, tzinfo=zoneinfo.ZoneInfo("UTC")), zoneinfo.ZoneInfo("UTC")
    )
