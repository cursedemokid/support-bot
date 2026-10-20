from __future__ import annotations

import pytest
from aiogram.types import FSInputFile

from app.media import resolve_media

PDF_LIMIT_BYTES = 50 * 1024 * 1024
PHOTO_LIMIT_BYTES = 10 * 1024 * 1024

PRODUCTION_ASSETS = {
    "asset://instructions/demo-brand-m815r45.pdf": (b"%PDF-", PDF_LIMIT_BYTES),
    "asset://instructions/demo-brand-w29a56.pdf": (b"%PDF-", PDF_LIMIT_BYTES),
    "asset://products/demo-brand-m815r45.png": (b"\x89PNG\r\n\x1a\n", PHOTO_LIMIT_BYTES),
    "asset://products/demo-brand-w29a56.png": (b"\x89PNG\r\n\x1a\n", PHOTO_LIMIT_BYTES),
}


def test_asset_reference_resolves_to_a_file_inside_packaged_assets() -> None:
    media = resolve_media("asset://products/demo.png")

    assert isinstance(media, FSInputFile)
    assert str(media.path).endswith("app/assets/products/demo.png")


def test_asset_reference_cannot_escape_packaged_assets() -> None:
    with pytest.raises(ValueError, match="Недопустим"):
        resolve_media("asset://../.env")


@pytest.mark.parametrize(("reference", "signature_and_limit"), PRODUCTION_ASSETS.items())
def test_production_assets_exist_and_fit_telegram_limits(
    reference: str,
    signature_and_limit: tuple[bytes, int],
) -> None:
    signature, limit = signature_and_limit
    media = resolve_media(reference)

    assert isinstance(media, FSInputFile)
    assert media.path.is_file()
    assert media.path.stat().st_size <= limit
    with media.path.open("rb") as asset:
        assert asset.read(len(signature)) == signature
