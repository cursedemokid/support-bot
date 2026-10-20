"""Сборка роутеров. Порядок регистрации = порядок проверки апдейта.

Менеджерский роутер идёт первым, но держится это не на порядке, а на
фильтрах: у менеджерских хендлеров на роутере висит `IsManager`, у
клиентских — `F.chat.type == PRIVATE`. Множества не пересекаются, и
перестановка местами ничего не меняет.

Внутри клиентского роутера порядок уже значим: catch-all на свободный текст
(`F.text` в user_flow) обязан стоять последним — всё, что встанет за ним, не
получит ни одного текстового апдейта, и выглядеть это будет не сбоем, а
тишиной. Стережёт `tests/test_wiring.py`.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.enums import ChatType

from app.bot.handlers import admin_content, admin_faq, manager_commands, user_flow


def build_root_router() -> Router:
    root = Router(name="root")
    root.include_router(admin_faq.router)
    root.include_router(admin_content.router)
    root.include_router(manager_commands.router)

    # Клиентский диалог — только в личке. Сотрудники обрабатываются отдельным
    # роутером выше и не попадают в клиентский сценарий.
    private = Router(name="private")
    private.message.filter(F.chat.type == ChatType.PRIVATE)
    private.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)
    private.include_router(user_flow.router)
    root.include_router(private)

    return root
