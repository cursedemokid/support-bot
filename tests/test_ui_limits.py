from __future__ import annotations

from html import unescape
from html.parser import HTMLParser
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.repositories import ui as ui_repo
from app.services.ui import UiPhoto, UiService, _split
from tests.factories import FakeBot


class ValidHtml(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.stack: list[str] = []
        self.text = ""

    def handle_starttag(self, tag: str, attrs) -> None:
        self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        assert self.stack.pop() == tag

    def handle_data(self, data: str) -> None:
        self.text += data


def test_long_html_keeps_entities_and_balanced_tags() -> None:
    body = "<b>" + "&#128578; &amp; &lt; " * 700 + "</b>"
    parts = _split(body)
    recovered = ""
    for part in parts:
        assert len(part) <= 4096
        parser = ValidHtml()
        parser.feed(part)
        assert parser.stack == []
        recovered += parser.text
    assert recovered == unescape("&#128578; &amp; &lt; " * 700)


def test_emoji_chunks_respect_utf16_limits_without_splitting_codepoints() -> None:
    text = "🙂" * 3000
    chunks = _split(text)
    assert "".join(chunks) == text
    assert all(len(chunk.encode("utf-16-le")) // 2 <= 4096 for chunk in chunks)


async def test_history_photo_with_long_caption_keeps_full_text(monkeypatch, clock) -> None:
    monkeypatch.setattr(ui_repo, "list_messages", AsyncMock(return_value=[]))
    monkeypatch.setattr(ui_repo, "forget_all", AsyncMock())
    monkeypatch.setattr(ui_repo, "remember", AsyncMock())
    bot = FakeBot()
    text = "Подробное описание " * 100
    await UiService(clock).render(
        SimpleNamespace(commit=AsyncMock()),
        bot,
        chat_id=42,
        parts=[UiPhoto("history-photo", text)],
    )
    assert len(bot.photos_to(42)[0].caption or "") <= 1024
    assert text in "".join(bot.texts_to(42))
