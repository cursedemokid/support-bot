"""Мини-админка контента: добавление товаров, инструкций и категорий."""

from __future__ import annotations

import re

from aiogram import Bot, F, Router, html
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.filters.access import IsAdmin
from app.bot.keyboards import (
    admin_menu,
    admin_product_keyboard,
    back_keyboard,
    list_keyboard,
    with_back,
)
from app.catalog import normalize_product_image_ids
from app.clock import Clock
from app.db.models import Product, StaffMember
from app.repositories import content as content_repo
from app.repositories import staff as staff_repo
from app.services.ui import UiService

router = Router(name="admin_content")
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())


class AdminFlow(StatesGroup):
    product_sku = State()
    product_title = State()
    product_description = State()
    product_photos = State()
    product_pdf = State()
    section_title = State()
    section_body = State()
    category_key = State()
    category_title = State()
    staff_username = State()
    staff_display_name = State()
    staff_edit_name = State()


async def _replace_after_input(
    message: Message,
    session: AsyncSession,
    bot: Bot,
    clock: Clock,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    ui = UiService(clock)
    await ui.remove_incoming(bot, message.chat.id, message.message_id)
    await ui.render(
        session,
        bot,
        chat_id=message.chat.id,
        parts=[text],
        reply_markup=reply_markup,
    )


async def _render_admin_product(
    session: AsyncSession,
    bot: Bot,
    clock: Clock,
    *,
    chat_id: int,
    product: Product,
) -> None:
    await UiService(clock).render(
        session,
        bot,
        chat_id=chat_id,
        parts=[
            f"<b>{html.quote(product.title)}</b>\nАртикул: <code>{html.quote(product.sku)}</code>"
        ],
        reply_markup=admin_product_keyboard(product.id),
    )


@router.callback_query(F.data == "adm:menu")
async def menu(
    query: CallbackQuery,
    session: AsyncSession,
    bot: Bot,
    clock: Clock,
    state: FSMContext,
) -> None:
    await state.clear()
    await query.answer()
    await UiService(clock).render(
        session,
        bot,
        chat_id=query.from_user.id,
        parts=["Управление контентом:"],
        reply_markup=admin_menu(),
    )


@router.callback_query(F.data == "adm:staff")
async def staff_menu(
    query: CallbackQuery,
    session: AsyncSession,
    bot: Bot,
    clock: Clock,
    state: FSMContext,
) -> None:
    await state.clear()
    rows = await staff_repo.all_staff(session)
    await query.answer()
    member_rows: list[tuple[int | str, str]] = [("add", "➕ Пригласить сотрудника")]
    member_rows.extend(
        (member.id, f"{'✅' if member.is_active else '⛔'} {member.display_name}")
        for member in rows
    )
    await UiService(clock).render(
        session,
        bot,
        chat_id=query.from_user.id,
        parts=["Сотрудники:"],
        reply_markup=with_back(
            "admstaff",
            member_rows,
            "mgr:home",
        ),
    )


@router.callback_query(F.data == "admstaff:add")
async def add_staff_start(
    query: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
    clock: Clock,
) -> None:
    await state.clear()
    await state.set_state(AdminFlow.staff_username)
    await query.answer()
    await UiService(clock).render(
        session,
        bot,
        chat_id=query.from_user.id,
        parts=["Введите Telegram @redacted_user сотрудника."],
        reply_markup=back_keyboard("adm:staff"),
    )


@router.message(AdminFlow.staff_username, F.text)
async def staff_username(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
    clock: Clock,
) -> None:
    username = staff_repo.normalize_username(message.text or "")
    if not username or not re.fullmatch(r"[a-z0-9_]{5,32}", username):
        await _replace_after_input(
            message,
            session,
            bot,
            clock,
            "Введите корректный @redacted_user.",
            reply_markup=back_keyboard("adm:staff"),
        )
        return
    await state.update_data(staff_username=username)
    await state.set_state(AdminFlow.staff_display_name)
    await _replace_after_input(
        message,
        session,
        bot,
        clock,
        "Введите отображаемый ник сотрудника.",
        reply_markup=back_keyboard("adm:staff"),
    )


@router.message(AdminFlow.staff_display_name, F.text)
async def staff_display_name(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
    clock: Clock,
) -> None:
    display_name = (message.text or "").strip()
    if not display_name:
        await _replace_after_input(
            message,
            session,
            bot,
            clock,
            "Ник не может быть пустым.",
            reply_markup=back_keyboard("adm:staff"),
        )
        return
    await state.update_data(staff_display_name=display_name)
    await _replace_after_input(
        message,
        session,
        bot,
        clock,
        "Выберите роль:",
        reply_markup=list_keyboard(
            "admstaffrole",
            [(staff_repo.SUPPORT, "🛠 Сотрудник"), (staff_repo.ADMIN, "⚙️ Администратор")],
        ),
    )


@router.callback_query(AdminFlow.staff_display_name, F.data.startswith("admstaffrole:"))
async def staff_role_selected(
    query: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    clock: Clock,
    bot: Bot,
) -> None:
    role = (query.data or "").split(":", 1)[1]
    data = await state.get_data()
    if role not in staff_repo.ROLES or not isinstance(data.get("staff_username"), str):
        await query.answer("Данные приглашения потеряны", show_alert=True)
        return
    try:
        await staff_repo.invite(
            session,
            username=data["staff_username"],
            display_name=str(data.get("staff_display_name", "")),
            role=role,
            now=clock.now(),
        )
        await session.commit()
    except IntegrityError:
        await session.rollback()
        await query.answer("Такой @redacted_user уже добавлен", show_alert=True)
        return
    await state.clear()
    await query.answer()
    await UiService(clock).render(
        session,
        bot,
        chat_id=query.from_user.id,
        parts=["Приглашение создано. Сотрудник должен отправить боту /start."],
        reply_markup=back_keyboard("adm:staff"),
    )


@router.callback_query(F.data.regexp(r"^admstaff:\d+$"))
async def staff_details(
    query: CallbackQuery, session: AsyncSession, bot: Bot, clock: Clock
) -> None:
    member_id = int((query.data or "").split(":", 1)[1])
    member = await session.get(StaffMember, member_id)
    if member is None:
        await query.answer("Сотрудник не найден", show_alert=True)
        return
    await query.answer()
    role_label = "Администратор" if member.role == staff_repo.ADMIN else "Сотрудник"
    next_role = staff_repo.ADMIN if member.role == staff_repo.SUPPORT else staff_repo.SUPPORT
    active_label = "Включить" if not member.is_active else "Отключить"
    await UiService(clock).render(
        session,
        bot,
        chat_id=query.from_user.id,
        parts=[
            f"<b>{html.quote(member.display_name)}</b> · @{html.quote(member.telegram_username)}\n"
            f"Роль: {role_label}"
        ],
        reply_markup=with_back(
            "admstaffedit",
            [
                (f"name:{member.id}", "✏️ Изменить ник"),
                (f"role:{member.id}:{next_role}", "Сменить роль"),
                (f"active:{member.id}:{int(not member.is_active)}", active_label),
            ],
            "adm:staff",
        ),
    )


@router.callback_query(F.data.regexp(r"^admstaffedit:name:\d+$"))
async def staff_edit_name_start(
    query: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
    clock: Clock,
) -> None:
    member_id = int((query.data or "").rsplit(":", 1)[1])
    await state.set_state(AdminFlow.staff_edit_name)
    await state.update_data(editing_staff_id=member_id)
    await query.answer()
    await UiService(clock).render(
        session,
        bot,
        chat_id=query.from_user.id,
        parts=["Введите новый отображаемый ник."],
        reply_markup=back_keyboard(f"admstaff:{member_id}"),
    )


@router.message(AdminFlow.staff_edit_name, F.text)
async def staff_edit_name(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    clock: Clock,
    bot: Bot,
) -> None:
    display_name = (message.text or "").strip()
    member_id = (await state.get_data()).get("editing_staff_id")
    if not display_name or not isinstance(member_id, int):
        await _replace_after_input(
            message,
            session,
            bot,
            clock,
            "Ник не может быть пустым.",
            reply_markup=back_keyboard("adm:staff"),
        )
        return
    await staff_repo.update_member(
        session, member_id=member_id, display_name=display_name, now=clock.now()
    )
    await session.commit()
    await state.clear()
    await _replace_after_input(
        message,
        session,
        bot,
        clock,
        "Ник сотрудника изменён.",
        reply_markup=back_keyboard("adm:staff"),
    )


@router.callback_query(F.data.regexp(r"^admstaffedit:(role|active):\d+:(admin|support|0|1)$"))
async def staff_update(query: CallbackQuery, session: AsyncSession, clock: Clock) -> None:
    _, action, member_id_raw, value = (query.data or "").split(":")
    kwargs = {"role": value} if action == "role" else {"is_active": value == "1"}
    await staff_repo.update_member(session, member_id=int(member_id_raw), now=clock.now(), **kwargs)
    await session.commit()
    await query.answer("Сотрудник обновлён")


@router.callback_query(F.data.in_({"adm:products", "adm:instructions", "adm:categories"}))
async def kind_menu(
    query: CallbackQuery,
    session: AsyncSession,
    bot: Bot,
    clock: Clock,
    state: FSMContext,
) -> None:
    await state.clear()
    kind = (query.data or "").split(":", 1)[1]
    labels = {"products": "товар", "instructions": "инструкцию", "categories": "категорию"}
    await query.answer()
    if kind == "products":
        products = await content_repo.find_products(session, "")
        rows: list[tuple[int | str, str]] = [(kind, "➕ Добавить")]
        rows.extend((product.id, f"{product.sku} · {product.title}") for product in products)
        await UiService(clock).render(
            session,
            bot,
            chat_id=query.from_user.id,
            parts=["Товары:"],
            reply_markup=with_back("admprod", rows, "adm:menu"),
        )
        return
    await UiService(clock).render(
        session,
        bot,
        chat_id=query.from_user.id,
        parts=[f"Добавьте {labels[kind]}:"],
        reply_markup=with_back("admadd", [(kind, "➕ Добавить")], "adm:menu"),
    )


@router.callback_query(F.data == "admadd:products")
async def add_product_start(
    query: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
    clock: Clock,
) -> None:
    await state.clear()
    await state.set_state(AdminFlow.product_sku)
    await query.answer()
    await UiService(clock).render(
        session,
        bot,
        chat_id=query.from_user.id,
        parts=["Введите артикул товара."],
        reply_markup=back_keyboard("adm:products"),
    )


@router.callback_query(F.data.regexp(r"^admprod:\d+$"))
async def product_actions(
    query: CallbackQuery,
    session: AsyncSession,
    bot: Bot,
    clock: Clock,
    state: FSMContext,
) -> None:
    await state.clear()
    product_id = int((query.data or "").split(":", 1)[1])
    product = await content_repo.product(session, product_id)
    if product is None:
        await query.answer("Товар не найден", show_alert=True)
        return
    await query.answer()
    await _render_admin_product(session, bot, clock, chat_id=query.from_user.id, product=product)


@router.callback_query(F.data.regexp(r"^admprodedit:(sku|title|description|photos|pdf):\d+$"))
async def edit_product_field_start(
    query: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
    clock: Clock,
) -> None:
    _, field, product_id_raw = (query.data or "").split(":")
    product_id = int(product_id_raw)
    if await content_repo.product(session, product_id) is None:
        await query.answer("Товар не найден", show_alert=True)
        return
    states = {
        "sku": AdminFlow.product_sku,
        "title": AdminFlow.product_title,
        "description": AdminFlow.product_description,
        "photos": AdminFlow.product_photos,
        "pdf": AdminFlow.product_pdf,
    }
    prompts = {
        "sku": "Введите новый артикул товара.",
        "title": "Введите новое название товара.",
        "description": "Введите новое описание товара.",
        "photos": "Пришлите от 1 до 3 новых фото. Они заменят текущие.",
        "pdf": "Пришлите новую PDF-инструкцию документом.",
    }
    await state.clear()
    await state.update_data(
        editing_product_id=product_id,
        editing_product_field=field,
        image_file_ids=[],
    )
    await state.set_state(states[field])
    await query.answer()
    await UiService(clock).render(
        session,
        bot,
        chat_id=query.from_user.id,
        parts=[prompts[field]],
        reply_markup=back_keyboard(f"admprod:{product_id}"),
    )


@router.callback_query(F.data.regexp(r"^admprodact:edit:\d+$"))
async def edit_product_start(
    query: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
    clock: Clock,
) -> None:
    product_id = int((query.data or "").rsplit(":", 1)[1])
    await state.clear()
    await state.update_data(editing_product_id=product_id)
    await state.set_state(AdminFlow.product_sku)
    await query.answer()
    await UiService(clock).render(
        session,
        bot,
        chat_id=query.from_user.id,
        parts=["Введите новый артикул товара."],
        reply_markup=back_keyboard(f"admprod:{product_id}"),
    )


@router.callback_query(F.data.regexp(r"^admprodact:delete:\d+$"))
async def delete_product_confirm(
    query: CallbackQuery, session: AsyncSession, bot: Bot, clock: Clock
) -> None:
    product_id = int((query.data or "").rsplit(":", 1)[1])
    await query.answer()
    await UiService(clock).render(
        session,
        bot,
        chat_id=query.from_user.id,
        parts=["Удалить товар без возможности восстановления?"],
        reply_markup=with_back(
            "admproddel",
            [(product_id, "✅ Удалить окончательно")],
            f"admprod:{product_id}",
        ),
    )


@router.callback_query(F.data.regexp(r"^admproddel:\d+$"))
async def delete_product_done(query: CallbackQuery, session: AsyncSession) -> None:
    product_id = int((query.data or "").split(":", 1)[1])
    deleted = await content_repo.delete_product(session, product_id)
    await session.commit()
    await query.answer("Товар удалён" if deleted else "Товар уже удалён")


@router.message(AdminFlow.product_sku, F.text)
async def product_sku(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
    clock: Clock,
) -> None:
    value = (message.text or "").strip()
    if await _save_text_product_field(
        message, state, session, bot, clock, field="sku", value=value
    ):
        return
    await state.update_data(sku=value)
    await state.set_state(AdminFlow.product_title)
    await _replace_after_input(
        message,
        session,
        bot,
        clock,
        "Введите название товара.",
        reply_markup=back_keyboard("adm:products"),
    )


@router.message(AdminFlow.product_title, F.text)
async def product_title(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
    clock: Clock,
) -> None:
    value = (message.text or "").strip()
    if await _save_text_product_field(
        message, state, session, bot, clock, field="title", value=value
    ):
        return
    await state.update_data(title=value)
    await state.set_state(AdminFlow.product_description)
    await _replace_after_input(
        message,
        session,
        bot,
        clock,
        "Введите описание товара.",
        reply_markup=back_keyboard("adm:products"),
    )


@router.message(AdminFlow.product_description, F.text)
async def product_description(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
    clock: Clock,
) -> None:
    value = (message.text or "").strip()
    if await _save_text_product_field(
        message, state, session, bot, clock, field="description", value=value
    ):
        return
    await state.update_data(description=value)
    await state.set_state(AdminFlow.product_photos)
    await _replace_after_input(
        message,
        session,
        bot,
        clock,
        "Пришлите от 1 до 3 фото товара. После последнего нажмите «Готово».",
        reply_markup=back_keyboard("adm:products"),
    )


@router.message(AdminFlow.product_photos, F.photo)
async def product_photo(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
    clock: Clock,
) -> None:
    photo = message.photo[-1] if message.photo else None
    if photo is None:
        await _replace_after_input(
            message,
            session,
            bot,
            clock,
            "Нужна фотография товара.",
            reply_markup=back_keyboard("adm:products"),
        )
        return
    data = await state.get_data()
    image_file_ids = list(data.get("image_file_ids", []))
    if len(image_file_ids) >= 3:
        await _replace_after_input(
            message,
            session,
            bot,
            clock,
            "Можно добавить не более трёх фото. Пришлите PDF-инструкцию.",
            reply_markup=back_keyboard("adm:products"),
        )
        await state.set_state(AdminFlow.product_pdf)
        return
    image_file_ids.append(photo.file_id)
    await state.update_data(image_file_ids=image_file_ids)
    if len(image_file_ids) == 3:
        if data.get("editing_product_field") == "photos":
            product_id = data.get("editing_product_id")
            if isinstance(product_id, int):
                updated = await content_repo.replace_product_images(
                    session,
                    product_id=product_id,
                    image_file_ids=image_file_ids,
                    now=clock.now(),
                )
                if updated is None:
                    await state.clear()
                    await _replace_after_input(
                        message,
                        session,
                        bot,
                        clock,
                        "Товар не найден.",
                        reply_markup=back_keyboard("adm:products"),
                    )
                    return
                await session.commit()
                await state.clear()
                await UiService(clock).remove_incoming(bot, message.chat.id, message.message_id)
                await _render_admin_product(
                    session, bot, clock, chat_id=message.chat.id, product=updated
                )
                return
        await state.set_state(AdminFlow.product_pdf)
        await _replace_after_input(
            message,
            session,
            bot,
            clock,
            "Добавлено 3 из 3 фото. Пришлите PDF-инструкцию документом.",
            reply_markup=back_keyboard("adm:products"),
        )
        return
    await _replace_after_input(
        message,
        session,
        bot,
        clock,
        f"Добавлено фото: {len(image_file_ids)} из 3. Добавьте ещё или нажмите «Готово».",
        reply_markup=with_back(
            "admadd",
            [("photos-done", "✅ Готово")],
            (
                f"admprod:{data['editing_product_id']}"
                if isinstance(data.get("editing_product_id"), int)
                else "adm:products"
            ),
        ),
    )


@router.callback_query(AdminFlow.product_photos, F.data == "admadd:photos-done")
async def product_photos_done(
    query: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
    clock: Clock,
) -> None:
    data = await state.get_data()
    try:
        normalize_product_image_ids(list(data.get("image_file_ids", [])))
    except ValueError:
        await query.answer("Добавьте хотя бы одно фото", show_alert=True)
        return
    if data.get("editing_product_field") == "photos":
        product_id = data.get("editing_product_id")
        if not isinstance(product_id, int):
            await query.answer("Товар не найден", show_alert=True)
            return
        updated = await content_repo.replace_product_images(
            session,
            product_id=product_id,
            image_file_ids=list(data["image_file_ids"]),
            now=clock.now(),
        )
        if updated is None:
            await query.answer("Товар не найден", show_alert=True)
            return
        await session.commit()
        await state.clear()
        await query.answer("Фотографии обновлены")
        await _render_admin_product(
            session, bot, clock, chat_id=query.from_user.id, product=updated
        )
        return
    await state.set_state(AdminFlow.product_pdf)
    await query.answer()
    await UiService(clock).render(
        session,
        bot,
        chat_id=query.from_user.id,
        parts=["Пришлите PDF-инструкцию документом."],
        reply_markup=back_keyboard("adm:products"),
    )


@router.message(AdminFlow.product_pdf, F.document)
async def product_pdf(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    clock: Clock,
    bot: Bot,
) -> None:
    doc = message.document
    if doc is None or not (
        doc.mime_type == "application/pdf" or (doc.file_name or "").lower().endswith(".pdf")
    ):
        await _replace_after_input(
            message,
            session,
            bot,
            clock,
            "Нужен документ в формате PDF.",
            reply_markup=back_keyboard("adm:products"),
        )
        return
    data = await state.get_data()
    if data.get("editing_product_field") == "pdf":
        product_id = data.get("editing_product_id")
        if not isinstance(product_id, int):
            await state.clear()
            await _replace_after_input(
                message,
                session,
                bot,
                clock,
                "Товар не найден.",
                reply_markup=back_keyboard("adm:products"),
            )
            return
        updated = await content_repo.update_product_field(
            session,
            product_id=product_id,
            field="pdf_file_id",
            value=doc.file_id,
            now=clock.now(),
        )
        if updated is None:
            await state.clear()
            await _replace_after_input(
                message,
                session,
                bot,
                clock,
                "Товар не найден.",
                reply_markup=back_keyboard("adm:products"),
            )
            return
        await session.commit()
        await state.clear()
        await UiService(clock).remove_incoming(bot, message.chat.id, message.message_id)
        await _render_admin_product(session, bot, clock, chat_id=message.chat.id, product=updated)
        return
    if not all(
        isinstance(data.get(key), str) and data[key] for key in ("sku", "title", "description")
    ):
        await state.clear()
        await _replace_after_input(
            message,
            session,
            bot,
            clock,
            "Данные потеряны. Начните добавление заново.",
            reply_markup=back_keyboard("adm:products"),
        )
        return
    try:
        image_file_ids = normalize_product_image_ids(list(data.get("image_file_ids", [])))
    except ValueError:
        await state.clear()
        await _replace_after_input(
            message,
            session,
            bot,
            clock,
            "Не удалось сохранить фото. Начните добавление товара заново.",
            reply_markup=back_keyboard("adm:products"),
        )
        return
    try:
        editing_product_id = data.get("editing_product_id")
        if isinstance(editing_product_id, int):
            updated = await content_repo.update_product(
                session,
                product_id=editing_product_id,
                sku=data["sku"],
                title=data["title"],
                description=data["description"],
                pdf_file_id=doc.file_id,
                image_file_ids=list(image_file_ids),
                now=clock.now(),
            )
            if updated is None:
                await _replace_after_input(
                    message,
                    session,
                    bot,
                    clock,
                    "Товар уже удалён.",
                    reply_markup=back_keyboard("adm:products"),
                )
                return
        else:
            await content_repo.create_product(
                session,
                sku=data["sku"],
                title=data["title"],
                description=data["description"],
                pdf_file_id=doc.file_id,
                image_file_ids=list(image_file_ids),
                now=clock.now(),
            )
        await session.commit()
    except IntegrityError:
        await session.rollback()
        await _replace_after_input(
            message,
            session,
            bot,
            clock,
            "Товар с таким артикулом уже есть.",
            reply_markup=back_keyboard("adm:products"),
        )
        return
    await state.clear()
    await _replace_after_input(
        message,
        session,
        bot,
        clock,
        "Товар сохранён вместе с фотографиями и PDF-инструкцией.",
        reply_markup=back_keyboard("adm:products"),
    )


async def _save_text_product_field(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
    clock: Clock,
    *,
    field: str,
    value: str,
) -> bool:
    data = await state.get_data()
    if data.get("editing_product_field") != field:
        return False
    product_id = data.get("editing_product_id")
    if not isinstance(product_id, int):
        await state.clear()
        await _replace_after_input(
            message,
            session,
            bot,
            clock,
            "Товар не найден.",
            reply_markup=back_keyboard("adm:products"),
        )
        return True
    if not value:
        await _replace_after_input(
            message,
            session,
            bot,
            clock,
            "Значение не может быть пустым.",
            reply_markup=back_keyboard(f"admprod:{product_id}"),
        )
        return True
    try:
        updated = await content_repo.update_product_field(
            session,
            product_id=product_id,
            field=field,  # type: ignore[arg-type]
            value=value,
            now=clock.now(),
        )
        if updated is None:
            await state.clear()
            await _replace_after_input(
                message,
                session,
                bot,
                clock,
                "Товар не найден.",
                reply_markup=back_keyboard("adm:products"),
            )
            return True
        await session.commit()
    except IntegrityError:
        await session.rollback()
        await _replace_after_input(
            message,
            session,
            bot,
            clock,
            "Товар с таким артикулом уже есть.",
            reply_markup=back_keyboard(f"admprod:{product_id}"),
        )
        return True
    await state.clear()
    await UiService(clock).remove_incoming(bot, message.chat.id, message.message_id)
    await _render_admin_product(session, bot, clock, chat_id=message.chat.id, product=updated)
    return True


@router.callback_query(F.data == "admadd:instructions")
async def add_section_start(
    query: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
    clock: Clock,
) -> None:
    await state.set_state(AdminFlow.section_title)
    await query.answer()
    await UiService(clock).render(
        session,
        bot,
        chat_id=query.from_user.id,
        parts=["Введите название раздела инструкции."],
        reply_markup=back_keyboard("adm:instructions"),
    )


@router.message(AdminFlow.section_title, F.text)
async def section_title(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
    clock: Clock,
) -> None:
    await state.update_data(title=(message.text or "").strip())
    await state.set_state(AdminFlow.section_body)
    await _replace_after_input(
        message,
        session,
        bot,
        clock,
        "Введите текст инструкции.",
        reply_markup=back_keyboard("adm:instructions"),
    )


@router.message(AdminFlow.section_body, F.text)
async def section_body(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
    clock: Clock,
) -> None:
    title = (await state.get_data()).get("title")
    if not isinstance(title, str) or not title or not (message.text or "").strip():
        await _replace_after_input(
            message,
            session,
            bot,
            clock,
            "Название и текст не могут быть пустыми.",
            reply_markup=back_keyboard("adm:instructions"),
        )
        return
    await content_repo.create_section(session, title=title, body=(message.text or "").strip())
    await session.commit()
    await state.clear()
    await _replace_after_input(
        message,
        session,
        bot,
        clock,
        "Раздел инструкции добавлен.",
        reply_markup=back_keyboard("adm:instructions"),
    )


@router.callback_query(F.data == "admadd:categories")
async def add_category_start(
    query: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
    clock: Clock,
) -> None:
    await state.set_state(AdminFlow.category_key)
    await query.answer()
    await UiService(clock).render(
        session,
        bot,
        chat_id=query.from_user.id,
        parts=["Введите ключ категории латиницей, например warranty."],
        reply_markup=back_keyboard("adm:categories"),
    )


@router.message(AdminFlow.category_key, F.text)
async def category_key(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
    clock: Clock,
) -> None:
    key = (message.text or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9_]{1,32}", key):
        await _replace_after_input(
            message,
            session,
            bot,
            clock,
            "Ключ: латинские буквы, цифры и _, не более 32 символов.",
            reply_markup=back_keyboard("adm:categories"),
        )
        return
    await state.update_data(key=key)
    await state.set_state(AdminFlow.category_title)
    await _replace_after_input(
        message,
        session,
        bot,
        clock,
        "Введите название категории для клиента.",
        reply_markup=back_keyboard("adm:categories"),
    )


@router.message(AdminFlow.category_title, F.text)
async def category_title(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
    clock: Clock,
) -> None:
    key = (await state.get_data()).get("key")
    title = (message.text or "").strip()
    if not isinstance(key, str) or not title:
        await _replace_after_input(
            message,
            session,
            bot,
            clock,
            "Название не может быть пустым.",
            reply_markup=back_keyboard("adm:categories"),
        )
        return
    try:
        await content_repo.create_category(session, key=key, title=title)
        await session.commit()
    except IntegrityError:
        await session.rollback()
        await _replace_after_input(
            message,
            session,
            bot,
            clock,
            "Такая категория уже есть.",
            reply_markup=back_keyboard("adm:categories"),
        )
        return
    await state.clear()
    await _replace_after_input(
        message,
        session,
        bot,
        clock,
        "Категория добавлена.",
        reply_markup=back_keyboard("adm:categories"),
    )
