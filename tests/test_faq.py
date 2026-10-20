"""FAQ хранится в БД и отключённые типы не выдаются клиенту."""

from io import BytesIO

import pytest
from openpyxl import load_workbook

from app.repositories import faq
from app.services.faq_excel import (
    SAVE,
    FaqQuestionRow,
    FaqTypeRow,
    FaqWorkbookData,
    FaqWorkbookError,
    apply_faq_workbook,
    build_faq_workbook,
    parse_faq_workbook,
)


def test_client_visibility_query_exists() -> None:
    assert hasattr(faq, "visible_item")


async def test_disabling_type_keeps_questions_as_orphans(session_factory) -> None:
    async with session_factory() as session, session.begin():
        kind = await faq.create_type(session, title="Доставка", type_id=41)
        item = await faq.create_item(
            session,
            type_id=kind.id,
            question="Когда привезут?",
            answer="Срок указан при оформлении.",
        )
        assert item.type_id == kind.id

    async with session_factory() as session, session.begin():
        assert [row.question for row in await faq.items(session, kind.id)] == ["Когда привезут?"]
        assert await faq.disable_type(session, kind.id) == 1

    async with session_factory() as session:
        assert await faq.types(session) == []
        stored = await faq.item(session, item.id)
        assert stored is not None and stored.is_active and stored.type_id is None
        assert [row.id for row in await faq.orphan_items(session)] == [item.id]


async def test_type_ids_item_deletion_and_assignment(session_factory) -> None:
    async with session_factory() as session, session.begin():
        first = await faq.create_type(session, title="Первый")
        second = await faq.create_type(session, title="Второй")
        item = await faq.create_item(
            session,
            type_id=first.id,
            question="Вопрос",
            answer="Ответ",
        )
        assert second.id == first.id + 1
        assert await faq.assign_item(session, item_id=item.id, type_id=second.id)
        assert await faq.disable_item(session, item.id)
        assert not await faq.disable_item(session, item.id)

    async with session_factory() as session, session.begin():
        assert not await faq.assign_item(session, item_id=item.id, type_id=999_999)
        assert await faq.disable_type(session, second.id) == 0
        assert not await faq.assign_item(session, item_id=item.id, type_id=second.id)


async def test_new_type_without_order_is_placed_after_active_types(session_factory) -> None:
    async with session_factory() as session, session.begin():
        await faq.create_type(session, title="Первый", sort_order=4)
        await faq.create_type(session, title="Второй", sort_order=9)
        created = await faq.create_type(session, title="Последний")

    assert created.sort_order == 10
    async with session_factory() as session:
        assert [row.title for row in await faq.types(session)] == [
            "Первый",
            "Второй",
            "Последний",
        ]


async def test_import_creates_type_and_question_in_one_transaction(session_factory) -> None:
    data = FaqWorkbookData(
        types=(FaqTypeRow(row=2, id=1, title="Гарантия", sort_order=20, action=SAVE),),
        questions=(
            FaqQuestionRow(
                row=2,
                id=None,
                type_id=1,
                question="Срок?",
                answer="Один год",
                sort_order=10,
                action=SAVE,
            ),
        ),
        prepared_type_ids=(2, 3),
    )
    async with session_factory() as session, session.begin():
        summary = await apply_faq_workbook(session, data)
    assert summary.created_types == 1
    assert summary.created_questions == 1

    async with session_factory() as session:
        assert [row.title for row in await faq.types(session)] == ["Гарантия"]
        assert [row.question for row in await faq.items(session, 1)] == ["Срок?"]


async def test_import_explains_that_new_question_id_must_be_blank(session_factory) -> None:
    async with session_factory() as session, session.begin():
        await faq.create_type(session, title="Технические", type_id=1)

    data = FaqWorkbookData(
        types=(FaqTypeRow(row=2, id=1, title="Технические", sort_order=0, action=SAVE),),
        questions=(
            FaqQuestionRow(
                row=3,
                id=2,
                type_id=1,
                question="Новый вопрос",
                answer="Новый ответ",
                sort_order=0,
                action=SAVE,
            ),
        ),
        prepared_type_ids=(),
        template_next_type_id=2,
    )

    with pytest.raises(FaqWorkbookError, match="для нового вопроса очистите «ID вопроса»"):
        async with session_factory() as session, session.begin():
            await apply_faq_workbook(session, data)


async def test_import_rejects_stale_type_ids_without_partial_changes(session_factory) -> None:
    async with session_factory() as session, session.begin():
        await faq.create_type(session, title="Существующий", type_id=1)

    data = FaqWorkbookData(
        types=(
            FaqTypeRow(row=2, id=1, title="Изменённый", sort_order=0, action=SAVE),
            FaqTypeRow(row=3, id=3, title="С дырой", sort_order=1, action=SAVE),
        ),
        questions=(),
        prepared_type_ids=(),
    )
    with pytest.raises(FaqWorkbookError, match="свежий шаблон"):
        async with session_factory() as session, session.begin():
            await apply_faq_workbook(session, data)

    async with session_factory() as session:
        assert [row.title for row in await faq.types(session)] == ["Существующий"]


async def test_import_rejects_prepared_id_occupied_after_download(session_factory) -> None:
    async with session_factory() as session, session.begin():
        original = await faq.create_type(session, title="Исходный", type_id=1)

    payload = build_faq_workbook(types=[original], questions=[], next_type_id=2, new_type_slots=2)
    workbook = load_workbook(BytesIO(payload))
    workbook["Типы"]["B3"] = "Из файла"
    workbook["Типы"]["C3"] = 10
    workbook["Типы"]["D3"] = SAVE
    filled = BytesIO()
    workbook.save(filled)
    workbook.close()
    data = parse_faq_workbook(filled.getvalue())

    async with session_factory() as session, session.begin():
        await faq.create_type(session, title="Параллельный", type_id=2)

    with pytest.raises(FaqWorkbookError, match="свежий шаблон"):
        async with session_factory() as session, session.begin():
            await apply_faq_workbook(session, data)

    async with session_factory() as session:
        assert {row.id: row.title for row in await faq.types(session)} == {
            1: "Исходный",
            2: "Параллельный",
        }


async def test_import_updates_and_deletes_without_touching_absent_rows(session_factory) -> None:
    async with session_factory() as session, session.begin():
        first = await faq.create_type(session, title="Первый", type_id=1)
        untouched = await faq.create_type(session, title="Не трогать", type_id=2)
        deleted_item = await faq.create_item(
            session, type_id=first.id, question="Удалить?", answer="Да"
        )

    data = FaqWorkbookData(
        types=(FaqTypeRow(row=2, id=1, title="Обновлён", sort_order=5, action=SAVE),),
        questions=(
            FaqQuestionRow(
                row=2,
                id=deleted_item.id,
                type_id=1,
                question=deleted_item.question,
                answer=deleted_item.answer,
                sort_order=0,
                action="УДАЛИТЬ",
            ),
        ),
        prepared_type_ids=(),
    )
    async with session_factory() as session, session.begin():
        summary = await apply_faq_workbook(session, data)
    assert summary.updated_types == 1
    assert summary.deleted_questions == 1

    async with session_factory() as session:
        rows = await faq.types(session)
        assert {row.id: row.title for row in rows} == {
            1: "Обновлён",
            untouched.id: "Не трогать",
        }


async def test_import_type_deletion_orphans_questions(session_factory) -> None:
    async with session_factory() as session, session.begin():
        kind = await faq.create_type(session, title="Старый", type_id=1)
        item = await faq.create_item(session, type_id=kind.id, question="Остаться?", answer="Да")

    data = FaqWorkbookData(
        types=(FaqTypeRow(row=2, id=1, title="Старый", sort_order=0, action="УДАЛИТЬ"),),
        questions=(),
        prepared_type_ids=(),
    )
    async with session_factory() as session, session.begin():
        summary = await apply_faq_workbook(session, data)
    assert summary.deleted_types == 1
    assert summary.orphaned_questions == 1

    async with session_factory() as session:
        assert [row.id for row in await faq.orphan_items(session)] == [item.id]
        assert await faq.visible_item(session, item.id) is None

    async with session_factory() as session, session.begin():
        replacement = await faq.create_type(session, title="Новый")
        assert await faq.assign_item(session, item_id=item.id, type_id=replacement.id)

    async with session_factory() as session:
        visible = await faq.visible_item(session, item.id)
        assert visible is not None and visible.type_id == replacement.id
