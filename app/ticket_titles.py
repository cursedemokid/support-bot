from __future__ import annotations


def make_ticket_title(category_title: str, first_message: str, *, limit: int = 140) -> str:
    """Короткое устойчивое название обращения для списков и уведомлений."""
    prefix = f"{category_title} — "
    text = " ".join(first_message.split()) or "Без описания"
    available = limit - len(prefix)
    if len(text) > available:
        text = f"{text[: max(1, available - 1)].rstrip()}…"
    return f"{prefix}{text}"
