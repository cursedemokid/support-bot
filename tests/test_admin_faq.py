"""Административные FAQ-типы и вопросы используют отдельные маршруты."""

from __future__ import annotations

from typing import cast

from aiogram import Bot
from aiogram.types import CallbackQuery, User

from app.bot.handlers import admin_faq
from app.bot.keyboards import (
    faq_admin_menu,
    faq_item_card_keyboard,
    faq_question_types_keyboard,
    faq_type_card_keyboard,
)
from app.repositories import faq
from app.services.faq_excel import build_faq_workbook, format_import_summary
from tests.factories import (
    MANAGER_ID,
    FakeBot,
    admin_callback,
    admin_fsm,
    broken_workbook_bytes,
    document_message,
    private_message,
)


def _callbacks(markup) -> list[str]:
    return [button.callback_data or "" for row in markup.inline_keyboard for button in row]


def _texts(markup) -> list[str]:
    return [button.text for row in markup.inline_keyboard for button in row]


def test_faq_admin_root_has_separate_lists() -> None:
    assert _callbacks(faq_admin_menu()) == [
        "admfaq:types",
        "admfaq:questions",
        "admfaq:download",
        "admfaq:upload",
        "adm:menu",
    ]


def test_question_type_picker_puts_orphans_first() -> None:
    markup = faq_question_types_keyboard([(7, "Доставка")], has_orphans=True)
    assert _texts(markup)[:2] == ["Без типа", "Доставка"]
    assert _callbacks(markup)[:2] == ["admfaqquestions:orphan", "admfaqquestions:7"]


def test_type_and_question_cards_have_separate_actions() -> None:
    type_callbacks = _callbacks(faq_type_card_keyboard(7))
    question_callbacks = _callbacks(faq_item_card_keyboard(11, type_id=7))

    assert type_callbacks == [
        "admfaqtypeorder:7",
        "admfaqtypedel:7",
        "admfaq:types",
    ]
    assert question_callbacks == [
        "admfaqitemmove:11",
        "admfaqitemdel:11",
        "admfaqquestions:7",
    ]
    assert not any("typedel" in value for value in question_callbacks)


async def test_type_is_created_after_title_without_order_prompt(session_factory, clock) -> None:
    bot = FakeBot()
    state = admin_fsm()
    await state.set_state(admin_faq.AdminFaqFlow.type_title)
    message = private_message("Новый тип", user_id=MANAGER_ID).as_(cast(Bot, bot))

    async with session_factory() as session:
        await faq.create_type(session, title="Существующий", sort_order=12)
        await session.commit()
        await admin_faq.add_type_title(message, session, cast(Bot, bot), clock, state)

    assert await state.get_state() is None
    async with session_factory() as session:
        rows = await faq.types(session)
    assert [(row.title, row.sort_order) for row in rows] == [
        ("Существующий", 12),
        ("Новый тип", 13),
    ]


async def test_type_order_can_be_changed_from_type_card(session_factory, clock) -> None:
    bot = FakeBot()
    state = admin_fsm()
    async with session_factory() as session:
        value = await faq.create_type(session, title="Доставка", sort_order=10)
        await session.commit()
        query = admin_callback(f"admfaqtypeorder:{value.id}").as_(cast(Bot, bot))
        await admin_faq.edit_type_order_start(query, session, cast(Bot, bot), clock, state)
        assert await state.get_state() == admin_faq.AdminFaqFlow.type_order.state

        message = private_message("-5", user_id=MANAGER_ID).as_(cast(Bot, bot))
        await admin_faq.edit_type_order(message, session, cast(Bot, bot), clock, state)

    async with session_factory() as session:
        stored = await faq.types(session)
    assert stored[0].sort_order == -5
    assert await state.get_state() is None


def test_faq_callbacks_reach_new_router() -> None:
    cases = [
        (admin_faq.show_type, "admfaqtype:7"),
        (admin_faq.show_questions, "admfaqquestions:orphan"),
        (admin_faq.show_item, "admfaqitem:11"),
        (admin_faq.assign_item, "admfaqitemassign:11:7"),
    ]
    for handler, data in cases:
        query = CallbackQuery(
            id="callback-id",
            from_user=User(id=1, is_bot=False, first_name="Admin"),
            chat_instance="chat-instance",
            data=data,
        )
        handler_object = next(
            item for item in admin_faq.router.callback_query.handlers if item.callback is handler
        )
        assert all(
            item.magic is not None and bool(item.magic.resolve(query))
            for item in handler_object.filters
        )


async def test_download_sends_xlsx_with_current_faq(session_factory, clock) -> None:
    bot = FakeBot()
    query = admin_callback("admfaq:download").as_(cast(Bot, bot))
    async with session_factory() as session:
        await admin_faq.download_excel(query, session, cast(Bot, bot), clock)
    document = bot.documents_to(MANAGER_ID)[-1]
    assert document.filename == "faq-template.xlsx"
    assert isinstance(document.data, bytes)


async def test_upload_start_sets_state_and_has_download_and_back(session_factory, clock) -> None:
    bot = FakeBot()
    state = admin_fsm()
    query = admin_callback("admfaq:upload").as_(cast(Bot, bot))
    async with session_factory() as session:
        await admin_faq.upload_excel_start(query, state, session, cast(Bot, bot), clock)
    assert await state.get_state() == admin_faq.AdminFaqFlow.excel_upload
    assert bot.buttons(bot.sent[-1]) == ["Скачать свежий шаблон", "⬅️ Назад"]


async def test_invalid_upload_keeps_database_unchanged(session_factory, clock) -> None:
    bot = FakeBot()
    state = admin_fsm()
    await state.set_state(admin_faq.AdminFaqFlow.excel_upload)
    message = document_message(
        broken_workbook_bytes(),
        filename="faq-template.xlsx",
        mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ).as_(cast(Bot, bot))
    async with session_factory() as session:
        original_types = [row.title for row in await faq.types(session)]
        await admin_faq.upload_excel(message, state, session, cast(Bot, bot), clock)
        stored_types = [row.title for row in await faq.types(session)]
    assert "не удалось прочитать XLSX" in bot.texts[-1]
    assert stored_types == original_types
    assert await state.get_state() == admin_faq.AdminFaqFlow.excel_upload


async def test_unexpected_xlsx_reader_error_is_reported_to_admin(
    session_factory, clock, monkeypatch
) -> None:
    """Сбой библиотеки чтения XLSX не должен оставлять документ без ответа."""
    bot = FakeBot()
    state = admin_fsm()
    await state.set_state(admin_faq.AdminFaqFlow.excel_upload)
    message = document_message(
        b"xlsx payload",
        filename="faq-template.xlsx",
        mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ).as_(cast(Bot, bot))

    def fail_to_parse(_payload: bytes):
        raise TypeError("broken workbook metadata")

    monkeypatch.setattr(admin_faq, "parse_faq_workbook", fail_to_parse)

    async with session_factory() as session:
        await admin_faq.upload_excel(message, state, session, cast(Bot, bot), clock)

    assert "Файл не загружен" in bot.texts[-1]
    assert "скачайте свежий шаблон" in bot.texts[-1].lower()
    assert await state.get_state() == admin_faq.AdminFaqFlow.excel_upload


async def test_valid_upload_reuses_transaction_opened_by_access_check(
    session_factory, clock
) -> None:
    bot = FakeBot()
    state = admin_fsm()
    await state.set_state(admin_faq.AdminFaqFlow.excel_upload)
    payload = build_faq_workbook(types=[], questions=[], next_type_id=1)
    message = document_message(
        payload,
        filename="faq-template.xlsx",
        mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ).as_(cast(Bot, bot))

    async with session_factory() as session:
        await faq.types(session)  # имитация запроса из IsAdmin
        assert session.in_transaction()
        await admin_faq.upload_excel(message, state, session, cast(Bot, bot), clock)

    assert "FAQ обновлён" in bot.texts[-1]
    assert await state.get_state() is None


async def test_valid_upload_opens_transaction_for_bootstrap_admin(session_factory, clock) -> None:
    bot = FakeBot()
    state = admin_fsm()
    await state.set_state(admin_faq.AdminFaqFlow.excel_upload)
    message = document_message(
        build_faq_workbook(types=[], questions=[], next_type_id=1),
        filename="faq-template.xlsx",
        mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ).as_(cast(Bot, bot))

    async with session_factory() as session:
        assert not session.in_transaction()
        await admin_faq.upload_excel(message, state, session, cast(Bot, bot), clock)

    assert "FAQ обновлён" in bot.texts[-1]
    assert await state.get_state() is None


def test_import_summary_text_contains_all_counters() -> None:
    from app.services.faq_excel import FaqImportSummary

    text = format_import_summary(
        FaqImportSummary(
            created_types=2,
            updated_types=1,
            created_questions=5,
            updated_questions=3,
            deleted_questions=1,
            orphaned_questions=4,
        )
    )
    assert text == (
        "FAQ обновлён.\n"
        "Типы: добавлено 2, изменено 1, удалено 0.\n"
        "Вопросы: добавлено 5, изменено 3, удалено 1.\n"
        "Без типа: 4."
    )
