from app.ticket_titles import make_ticket_title


def test_ticket_title_combines_category_and_first_message_words() -> None:
    assert make_ticket_title(
        "Не работает / брак", "  Не включается после зарядки уже второй день.  "
    ) == ("Не работает / брак — Не включается после зарядки уже второй день.")


def test_ticket_title_truncates_long_first_message() -> None:
    title = make_ticket_title("Другое", "слово " * 40)

    assert title.startswith("Другое — ")
    assert title.endswith("…")
    assert len(title) <= 140
