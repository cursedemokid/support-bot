"""Безопасное разрешение ссылок на медиа для Telegram."""

from __future__ import annotations

from pathlib import Path

from aiogram.types import FSInputFile

ASSET_PREFIX = "asset://"
ASSETS_DIR = Path(__file__).parent / "assets"


def resolve_media(reference: str) -> str | FSInputFile:
    """Вернуть Telegram file_id либо файл, упакованный вместе с приложением."""

    if not reference.startswith(ASSET_PREFIX):
        return reference
    relative_path = reference.removeprefix(ASSET_PREFIX)
    asset_path = (ASSETS_DIR / relative_path).resolve()
    try:
        asset_path.relative_to(ASSETS_DIR.resolve())
    except ValueError as exc:
        msg = "Недопустимый путь к медиафайлу."
        raise ValueError(msg) from exc
    return FSInputFile(asset_path)
