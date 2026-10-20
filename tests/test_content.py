from __future__ import annotations

from app.repositories import content as content_repo


async def test_catalog_search_and_instruction_section(session_factory, clock) -> None:
    async with session_factory() as session, session.begin():
        product = await content_repo.create_product(
            session,
            sku="QW-100",
            title="DemoBrand One",
            description="Описание",
            pdf_file_id="telegram-file-id",
            now=clock.now(),
        )
        section = await content_repo.create_section(session, title="Уход", body="Текст ухода")

    async with session_factory() as session:
        found = await content_repo.find_products(session, "one")
        stored_section = await content_repo.section(session, section.id)

    assert [item.id for item in found] == [product.id]
    assert stored_section is not None and stored_section.body == "Текст ухода"
