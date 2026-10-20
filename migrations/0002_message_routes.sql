-- Маршруты сообщений: какие сообщения бота в чате менеджеров к какому тикету
-- относятся. Нужны, чтобы понять тикет по ответу менеджера (reply) — Telegram
-- в апдейте отдаёт только message_id сообщения, на которое ответили.
--
-- Записи удаляются каскадом вместе с тикетом. Чистка старых записей не нужна:
-- переписка по закрытым тикетам тоже должна отвечаться, а строк на тикет —
-- единицы.

create table if not exists support.message_routes (
    chat_id bigint not null,
    message_id bigint not null,
    ticket_id bigint not null references support.tickets (id) on delete cascade,
    created_at timestamptz not null,
    primary key (chat_id, message_id)
);

create index message_routes_ticket_idx on support.message_routes (ticket_id);

comment on table support.message_routes is
    'Сообщение бота в чате менеджеров → тикет: по нему распознаётся ответ менеджера';
