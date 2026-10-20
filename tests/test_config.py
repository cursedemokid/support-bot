"""Конфигурация: валидация, горячая перезагрузка, сверка боевого файла с эталоном."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.config import ConfigError, load_settings
from tests.conftest import REAL_SETTINGS, REFERENCE_SETTINGS


def _base() -> dict:
    import yaml

    return yaml.safe_load(REFERENCE_SETTINGS.read_text(encoding="utf-8"))


def test_real_settings_are_valid() -> None:
    """Боевой config/settings.yaml обязан читаться: это то, что уедет на VPS."""
    settings = load_settings(REAL_SETTINGS)
    assert settings.support.categories


def test_real_file_matches_reference_keys() -> None:
    """У боевого и эталонного файла один набор ключей.

    Расхождение значит, что ключ добавили только в один из них — тесты
    поведения перестали проверять то, что реально настроено.
    """
    real = load_settings(REAL_SETTINGS)
    reference = load_settings(REFERENCE_SETTINGS)
    assert [c.key for c in real.support.categories] == [c.key for c in reference.support.categories]
    assert set(real.model_dump()) == set(reference.model_dump())
    assert set(real.support.model_dump()) == set(reference.support.model_dump())


def test_duplicate_category_keys_rejected(write_settings: object) -> None:
    data = _base()
    data["support"]["categories"] = [
        {"key": "defect", "title": "A"},
        {"key": "defect", "title": "B"},
    ]
    path = write_settings(data)  # type: ignore[operator]
    with pytest.raises(ConfigError, match="дубликаты"):
        load_settings(path)


@pytest.mark.parametrize("key", ["DEFECT", "with space", "with:colon", "", "a" * 33])
def test_bad_category_key_rejected(write_settings: object, key: str) -> None:
    data = _base()
    data["support"]["categories"] = [{"key": key, "title": "Категория"}]
    path = write_settings(data)  # type: ignore[operator]
    with pytest.raises(ConfigError):
        load_settings(path)


def test_empty_legacy_categories_are_allowed(write_settings: object) -> None:
    data = _base()
    data["support"]["categories"] = []
    path = write_settings(data)  # type: ignore[operator]
    assert load_settings(path).support.categories == ()


def test_unknown_timezone_rejected(write_settings: object) -> None:
    data = _base()
    data["timezone"] = "Mars/Olympus"
    path = write_settings(data)  # type: ignore[operator]
    with pytest.raises(ConfigError, match="часовой пояс"):
        load_settings(path)


def test_unknown_key_rejected(write_settings: object) -> None:
    """Опечатка в имени ключа не должна проходить молча."""
    data = _base()
    data["support"]["manager_chat_idd"] = 1
    path = write_settings(data)  # type: ignore[operator]
    with pytest.raises(ConfigError):
        load_settings(path)


def test_obsolete_manager_chat_setting_is_rejected(write_settings: object) -> None:
    """Группа больше не используется: карточки приходят сотрудникам лично."""
    data = _base()
    data["support"]["manager_chat_id"] = -900000001
    path = write_settings(data)  # type: ignore[operator]
    with pytest.raises(ConfigError, match="manager_chat_id"):
        load_settings(path)


def test_missing_file_is_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="не найден"):
        load_settings(tmp_path / "nope.yaml")


def test_reload_applies_change(app_config, base_settings: dict) -> None:
    """Правка файла применяется к следующему обращению, без перезапуска."""
    import yaml

    assert len(app_config.settings.support.manager_user_ids) == 2
    assert app_config.path is not None

    base_settings["support"]["manager_user_ids"] = [999]
    app_config.path.write_text(yaml.safe_dump(base_settings, allow_unicode=True), encoding="utf-8")

    assert app_config.reload_if_changed() is True
    assert app_config.settings.support.manager_user_ids == (999,)


def test_reload_detects_content_change_when_mtime_is_unchanged(
    app_config, base_settings: dict
) -> None:
    """Файловая система может сохранить прежний mtime для быстрой перезаписи."""
    import yaml

    assert app_config.path is not None
    original = app_config.path.stat()
    base_settings["support"]["manager_user_ids"] = [900000003]
    app_config.path.write_text(yaml.safe_dump(base_settings, allow_unicode=True), encoding="utf-8")
    os.utime(
        app_config.path,
        ns=(original.st_atime_ns, original.st_mtime_ns),
    )

    assert app_config.reload_if_changed() is True
    assert app_config.settings.support.manager_user_ids == (900000003,)


def test_main_enables_settings_hot_reload(
    tmp_path: Path,
    clean_env: pytest.MonkeyPatch,
    monkeypatch: pytest.MonkeyPatch,
    base_settings: dict,
) -> None:
    """Боевой entrypoint передаёт AppConfig путь реально загруженного YAML."""
    import yaml

    from app import __main__ as entrypoint

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "settings.yaml").write_text(
        yaml.safe_dump(base_settings, allow_unicode=True), encoding="utf-8"
    )
    clean_env.setenv("BOT_TOKEN", "123:TEST")
    clean_env.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(entrypoint, "setup_logging", lambda **_kwargs: None)
    captured = []

    async def capture_config(config) -> None:
        captured.append(config)

    monkeypatch.setattr(entrypoint, "run", capture_config)

    entrypoint.main()

    assert captured[0].path == Path("config/settings.yaml")


def test_reload_of_broken_file_keeps_previous(app_config, base_settings: dict) -> None:
    """Битый YAML не роняет бота: остаётся прежняя конфигурация."""
    before = app_config.settings.support.manager_user_ids
    assert app_config.path is not None
    app_config.path.write_text("support: [это не словарь", encoding="utf-8")

    assert app_config.reload_if_changed() is False
    assert app_config.settings.support.manager_user_ids == before


def test_broken_file_reported_once(app_config) -> None:
    """Один и тот же битый файл не спамит лог на каждое сообщение."""
    assert app_config.path is not None
    app_config.path.write_text("broken: [", encoding="utf-8")
    assert app_config.reload_if_changed() is False
    assert app_config.reload_if_changed() is False


def test_database_secrets_need_only_url(clean_env, monkeypatch, tmp_path: Path) -> None:
    """Миграции не должны требовать BOT_TOKEN: схему накатывают до заведения бота."""
    from app.config import load_database_secrets

    # Из каталога без .env: иначе pydantic-settings подставит токен из файла
    # репозитория и тест перестанет проверять то, о чём заявлен.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@h:5432/db")
    assert load_database_secrets().database_url.startswith("postgresql")


def test_database_secrets_reject_placeholder(clean_env, monkeypatch, tmp_path: Path) -> None:
    from app.config import load_database_secrets

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", "YOUR_DB_URL")
    with pytest.raises(ConfigError, match="плейсхолдер"):
        load_database_secrets()


def test_bot_secrets_still_require_token(clean_env, monkeypatch, tmp_path: Path) -> None:
    from app.config import load_secrets

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@h:5432/db")
    with pytest.raises(ConfigError):
        load_secrets()
