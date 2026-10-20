-- Контент, которым администратор управляет из Telegram.

create table support.categories (
    key text primary key check (key ~ '^[a-z0-9_]{1,32}$'),
    title text not null check (length(title) between 1 and 100),
    is_active boolean not null default true,
    sort_order integer not null default 0
);

insert into support.categories (key, title, sort_order) values
  ('defect', 'Не работает / брак', 10),
  ('usage', 'Как пользоваться', 20),
  ('payment', 'Оплата и возврат', 30),
  ('other', 'Другое', 40)
on conflict (key) do nothing;

create table support.instruction_sections (
    id bigserial primary key,
    title text not null check (length(title) between 1 and 100),
    body text not null check (length(body) between 1 and 4000),
    sort_order integer not null default 0,
    is_active boolean not null default true
);

create table support.products (
    id bigserial primary key,
    sku text not null unique check (length(sku) between 1 and 100),
    title text not null check (length(title) between 1 and 200),
    description text not null check (length(description) between 1 and 4000),
    pdf_file_id text not null check (length(pdf_file_id) between 1 and 512),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index products_search_idx on support.products (lower(sku), lower(title));
