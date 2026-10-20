-- Внутренний UUID обращения, состояние отрисованного экрана и FAQ.
-- UUID не является публичным номером и никогда не показывается в Telegram.

alter table support.tickets add column guid uuid;

-- Не полагаемся на расширение pgcrypto: миграция должна работать на чистом
-- PostgreSQL. Значение строится из случайного MD5 и приводится к uuid.
update support.tickets
set guid = (
    substr(md5(random()::text || clock_timestamp()::text || id::text), 1, 8) || '-' ||
    substr(md5(random()::text || clock_timestamp()::text || id::text), 9, 4) || '-' ||
    substr(md5(random()::text || clock_timestamp()::text || id::text), 13, 4) || '-' ||
    substr(md5(random()::text || clock_timestamp()::text || id::text), 17, 4) || '-' ||
    substr(md5(random()::text || clock_timestamp()::text || id::text), 21, 12)
)::uuid
where guid is null;

alter table support.tickets alter column guid set not null;
create unique index tickets_guid_uidx on support.tickets (guid);

-- Сообщения, созданные текущим экраном бота. Перед следующей отрисовкой они
-- удаляются; старые сообщения до появления этой таблицы трогать невозможно.
create table support.chat_ui_messages (
    chat_id bigint not null,
    message_id bigint not null,
    created_at timestamptz not null default now(),
    primary key (chat_id, message_id)
);

create table support.faq_types (
    id bigserial primary key,
    title text not null check (length(title) between 1 and 100),
    is_active boolean not null default true,
    sort_order integer not null default 0
);

create table support.faq_items (
    id bigserial primary key,
    type_id bigint not null references support.faq_types (id) on delete restrict,
    question text not null check (length(question) between 1 and 500),
    answer text not null check (length(answer) between 1 and 4000),
    is_active boolean not null default true,
    sort_order integer not null default 0
);

create index faq_items_type_idx on support.faq_items (type_id, sort_order)
    where is_active;
