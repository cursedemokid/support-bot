from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import cast

from aiogram import Bot

from app.bot.handlers import admin_content, manager_commands, user_flow
from app.db.models import Product, StaffMember
from app.services.ui import UiPhoto
from tests.factories import MANAGER_ID, FakeBot, admin_callback


def _product() -> Product:
    return Product(
        id=17,
        sku='SKU<&"',
        title='Title <b>bold</b> & "quoted"',
        description="Description <i>markup</i> & <tag>",
        pdf_file_id="instruction.pdf",
        created_at=dt.datetime.now(dt.UTC),
        updated_at=dt.datetime.now(dt.UTC),
    )


def test_catalog_product_fields_are_escaped_without_escaping_template_markup() -> None:
    from app.catalog import product_card_parts

    card = product_card_parts(_product(), ["photo"])

    assert isinstance(card[0], UiPhoto)
    assert card[-1] == (
        '<b>Title &lt;b&gt;bold&lt;/b&gt; &amp; "quoted"</b>\n'
        'Артикул: <code>SKU&lt;&amp;"</code>\n\n'
        "Description &lt;i&gt;markup&lt;/i&gt; &amp; &lt;tag&gt;"
    )


async def test_admin_product_card_escapes_title_and_sku(monkeypatch, clock) -> None:
    rendered: dict[str, object] = {}

    class UiCapture:
        def __init__(self, _clock) -> None:
            pass

        async def render(self, _session, _bot, **kwargs) -> None:
            rendered.update(kwargs)

    monkeypatch.setattr(admin_content, "UiService", UiCapture)
    await admin_content._render_admin_product(
        None, cast(Bot, FakeBot()), clock, chat_id=MANAGER_ID, product=_product()
    )

    assert rendered["parts"] == [
        '<b>Title &lt;b&gt;bold&lt;/b&gt; &amp; "quoted"</b>\nАртикул: <code>SKU&lt;&amp;"</code>'
    ]


async def test_employee_pdf_caption_escapes_product_title(monkeypatch) -> None:
    bot = FakeBot()
    product = _product()

    async def get_product(_session, _product_id):
        return product

    monkeypatch.setattr(manager_commands.content_repo, "product", get_product)
    query = admin_callback("pdf:17").as_(cast(Bot, bot))

    await manager_commands.manager_product_pdf(query, None, cast(Bot, bot))

    assert bot.documents_to(MANAGER_ID)[-1].caption == (
        'Инструкция: Title &lt;b&gt;bold&lt;/b&gt; &amp; "quoted"'
    )


async def test_admin_staff_card_escapes_display_name_and_username(monkeypatch, clock) -> None:
    member = StaffMember(
        id=4,
        display_name="Name <&>",
        telegram_username='user<&"',
        role="support",
        is_active=True,
    )

    class SessionCapture:
        async def get(self, _model, _member_id):
            return member

    rendered: dict[str, object] = {}

    class UiCapture:
        def __init__(self, _clock) -> None:
            pass

        async def render(self, _session, _bot, **kwargs) -> None:
            rendered.update(kwargs)

    monkeypatch.setattr(admin_content, "UiService", UiCapture)
    bot = FakeBot()
    query = admin_callback("admstaff:4").as_(cast(Bot, bot))

    await admin_content.staff_details(query, SessionCapture(), cast(Bot, bot), clock)

    assert rendered["parts"] == ['<b>Name &lt;&amp;&gt;</b> · @user&lt;&amp;"\nРоль: Сотрудник']


async def test_faq_answer_escapes_question_and_answer(monkeypatch, clock) -> None:
    value = SimpleNamespace(question="Q <&>", answer="A <&>", type_id=9)

    async def visible_item(_session, _item_id):
        return value

    rendered: dict[str, object] = {}

    class UiCapture:
        async def render(self, _session, _bot, **kwargs) -> None:
            rendered.update(kwargs)

    monkeypatch.setattr(user_flow.faq_repo, "visible_item", visible_item)
    monkeypatch.setattr(user_flow, "_ui", lambda _clock: UiCapture())
    bot = FakeBot()
    query = admin_callback("faq:2").as_(cast(Bot, bot))

    await user_flow.faq_answer(query, None, cast(Bot, bot), clock)

    assert rendered["parts"] == ["<b>Q &lt;&amp;&gt;</b>\n\nA &lt;&amp;&gt;"]


async def test_instruction_body_escapes_title_and_body(monkeypatch, clock) -> None:
    value = SimpleNamespace(title="Title <&>", body="Body <&>")

    async def get_section(_session, _section_id):
        return value

    rendered: dict[str, object] = {}

    class UiCapture:
        async def render(self, _session, _bot, **kwargs) -> None:
            rendered.update(kwargs)

    monkeypatch.setattr(user_flow.content_repo, "section", get_section)
    monkeypatch.setattr(user_flow, "_ui", lambda _clock: UiCapture())
    bot = FakeBot()
    query = admin_callback("ins:3").as_(cast(Bot, bot))

    await user_flow.show_instruction(query, None, cast(Bot, bot), clock)

    assert rendered["parts"] == ["<b>Title &lt;&amp;&gt;</b>\n\nBody &lt;&amp;&gt;"]
