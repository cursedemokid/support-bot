-- Схема поддержки: тикеты, история сообщений, категории в конфиге (не в БД).
--
-- Применяется командой: uv run python -m app.migrate
-- Повторный запуск — идемпотентен, уже применённое не накатывается заново.

create schema if not exists support;

-- Тикеты клиентов. state: new → assigned → closed.
-- category — ключ из config/settings.yaml (без заголовка и эмодзи).
create table if not exists support.tickets (
    id bigserial primary key,
    telegram_user_id bigint not null,
    telegram_username text,        -- nullable: не у всех есть
    category text not null,
    description text not null,
    state text not null default 'new',
    manager_id bigint,             -- кто взял
    created_at timestamptz not null,
    assigned_at timestamptz,
    closed_at timestamptz,

    constraint tickets_state_valid check (state in ('new', 'assigned', 'closed')),
    constraint tickets_assigned_requires_manager check (
        (state = 'assigned' and manager_id is not null)
        or (state != 'assigned')
    ),
    -- У закрытого всегда есть тот, кто закрыл. Именно manager_id, а не
    -- assigned_at: спам-обращение закрывают, не беря в работу, и требовать
    -- ради этого лишний шаг «сначала возьми» — работа на пустом месте.
    constraint tickets_closed_requires_manager check (
        (state = 'closed' and manager_id is not null)
        or (state != 'closed')
    )
);

create index tickets_state_created_idx on support.tickets (state, created_at)
    where state in ('new', 'assigned');

create index tickets_manager_idx on support.tickets (manager_id)
    where manager_id is not null;

-- История переписки: клиент ↔ менеджер. Текст не логируется,
-- но хранится здесь для отображения менеджеру.
create table if not exists support.message_history (
    id bigserial primary key,
    ticket_id bigint not null references support.tickets (id) on delete cascade,
    from_user_id bigint not null,  -- кто отправил
    message_text text not null,
    sent_at timestamptz not null
);

create index message_history_ticket_idx on support.message_history (ticket_id, sent_at);

comment on table support.tickets is
    'Тикеты клиентов: создаются через бот, берутся и закрываются менеджерами';
comment on table support.message_history is
    'История переписки по тикету: первое сообщение — описание от клиента, дальше — ответы';
comment on column support.tickets.category is
    'Ключ из config/settings.yaml (defect, usage, payment, other)';
comment on column support.tickets.state is
    'new → assigned → closed';
