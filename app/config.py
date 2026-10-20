"""Конфигурация: YAML с настройками + секреты из .env.

Перечитывается на лету (см. AppConfig.reload_if_changed): правка категорий
или списка менеджеров применяется к следующему апдейту, перезапуск не нужен.
Битый файл перезагрузку не ломает: бот остаётся на прежней конфигурации
и один раз пишет ошибку в лог.
"""

from __future__ import annotations

import hashlib
import zoneinfo
from pathlib import Path
from typing import Annotated

import yaml
from loguru import logger
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

PositiveInt = Annotated[int, Field(gt=0)]
NonNegativeInt = Annotated[int, Field(ge=0)]


class ConfigError(Exception):
    """Конфиг невалиден. Текст рассчитан на человека, который его правит."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CategoryConfig(_Strict):
    """Категория обращения. key уходит в БД — после запуска не менять."""

    key: str = Field(min_length=1, max_length=32, pattern=r"^[a-z0-9_]+$")
    title: str = Field(min_length=1, max_length=100)


class SupportConfig(_Strict):
    # Оставлены временно совместимыми с существующим YAML: после миграции
    # категории берутся из PostgreSQL и правятся администратором в боте.
    categories: tuple[CategoryConfig, ...] = Field(default=())
    manager_user_ids: tuple[int, ...]
    admin_user_ids: tuple[int, ...] = ()
    max_message_chars: PositiveInt
    start_payload_max_chars: PositiveInt

    @field_validator("categories")
    @classmethod
    def _no_duplicate_keys(cls, value: tuple[CategoryConfig, ...]) -> tuple[CategoryConfig, ...]:
        keys = [cat.key for cat in value]
        if len(set(keys)) != len(keys):
            msg = "support.categories: дубликаты в ключах категорий"
            raise ValueError(msg)
        return value


class Settings(_Strict):
    """Дерево config/settings.yaml целиком."""

    timezone: str
    support: SupportConfig

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        try:
            zoneinfo.ZoneInfo(value)
        except zoneinfo.ZoneInfoNotFoundError:
            msg = f"timezone: часовой пояс {value!r} не найден в базе IANA"
            raise ValueError(msg) from None
        return value

    @property
    def tzinfo(self) -> zoneinfo.ZoneInfo:
        return zoneinfo.ZoneInfo(self.timezone)


class Secrets(BaseSettings):
    """Только из окружения и .env. В YAML и в репозитории их нет."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    bot_token: str = Field(min_length=1)
    database_url: str = Field(min_length=1)
    sentry_dsn: str | None = None
    log_level: str = "INFO"

    @field_validator("bot_token", "database_url")
    @classmethod
    def _not_a_placeholder(cls, value: str, info: ValidationInfo) -> str:
        if value.startswith("YOUR_") or value.startswith("<"):
            name = info.field_name or "значение"
            msg = f"{name}: в окружении остался плейсхолдер из .env.example ({value!r})"
            raise ValueError(msg)
        return value


class DatabaseSecrets(BaseSettings):
    """Что нужно раннеру миграций: один DATABASE_URL и ничего больше.

    Отдельный класс, а не `Secrets`: `python -m app.migrate` не имеет дела до
    токена бота, и требовать его — значит запрещать накатывать схему до того,
    как заведён бот. Ровно об этом сообщение «bot_token: Field required»
    при первом же запуске миграций в контейнере.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    database_url: str = Field(min_length=1)

    @field_validator("database_url")
    @classmethod
    def _not_a_placeholder(cls, value: str) -> str:
        if value.startswith("YOUR_") or value.startswith("<"):
            msg = f"database_url: в окружении остался плейсхолдер из .env.example ({value!r})"
            raise ValueError(msg)
        return value


class AppConfig:
    """Конфигурация приложения. `settings` перечитывается на лету.

    Битый файл перезагрузку не ломает: остаётся прежняя конфигурация,
    в лог уходит ошибка — один раз, а не на каждое сообщение.
    """

    __slots__ = (
        "_failed_fingerprint",
        "_loaded_fingerprint",
        "_path",
        "_secrets",
        "_settings",
    )

    def __init__(self, settings: Settings, secrets: Secrets, path: Path | None = None) -> None:
        self._settings = settings
        self._secrets = secrets
        self._path = path
        self._loaded_fingerprint = self._fingerprint()
        self._failed_fingerprint: bytes | None = None

    @property
    def settings(self) -> Settings:
        return self._settings

    @property
    def secrets(self) -> Secrets:
        return self._secrets

    @property
    def path(self) -> Path | None:
        return self._path

    def _fingerprint(self) -> bytes | None:
        if self._path is None:
            return None
        try:
            return hashlib.sha256(self._path.read_bytes()).digest()
        except OSError:
            return None

    def reload_if_changed(self) -> bool:
        """Перечитать файл, если он изменился. `True` — конфигурация обновилась."""
        if self._path is None:
            return False

        fingerprint = self._fingerprint()
        if fingerprint is None or fingerprint == self._loaded_fingerprint:
            return False
        if fingerprint == self._failed_fingerprint:
            # Про этот же битый файл уже говорили — молчим до следующей правки.
            return False

        try:
            settings = load_settings(self._path)
        except ConfigError as exc:
            self._failed_fingerprint = fingerprint
            logger.error("Конфигурация не перечитана, работаем на прежней:\n{}", exc)
            return False

        self._settings = settings
        self._loaded_fingerprint = fingerprint
        self._failed_fingerprint = None
        logger.info(
            "Конфигурация перечитана: менеджеров {}, администраторов {}",
            len(settings.support.manager_user_ids),
            len(settings.support.admin_user_ids),
        )
        return True


def _readable(error: ValidationError, source: str) -> str:
    """Pydantic ValidationError → человеческий текст с именами полей."""
    lines = [f"Конфигурация из {source} невалидна:"]
    for err in error.errors():
        loc = ".".join(str(x) for x in err["loc"])
        lines.append(f"  {loc}: {err['msg']}")
    return "\n".join(lines)


def load_settings(path: Path = Path("config/settings.yaml")) -> Settings:
    """Прочитать и валидировать YAML. ConfigError — файла нет или он битый."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        msg = f"Файл конфигурации не найден: {path}"
        raise ConfigError(msg) from exc
    except (yaml.YAMLError, OSError) as exc:
        msg = f"Не удалось прочитать {path}:\n  {exc}"
        raise ConfigError(msg) from exc

    try:
        return Settings.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(_readable(exc, str(path))) from None


def load_secrets() -> Secrets:
    """Прочитать секреты бота из окружения и .env. ConfigError — невалидны."""
    try:
        return Secrets()
    except ValidationError as exc:
        raise ConfigError(_readable(exc, "окружение и .env")) from None


def load_database_secrets() -> DatabaseSecrets:
    """Секреты для миграций: только DATABASE_URL."""
    try:
        return DatabaseSecrets()
    except ValidationError as exc:
        raise ConfigError(_readable(exc, "окружение и .env")) from None
