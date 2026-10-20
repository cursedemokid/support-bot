-- FAQ type removal must preserve its questions for later reassignment.

alter table support.faq_items
    drop constraint if exists faq_items_type_id_fkey;

alter table support.faq_items
    alter column type_id drop not null;

alter table support.faq_items
    add constraint faq_items_type_id_fkey
    foreign key (type_id) references support.faq_types (id) on delete set null;

create index if not exists faq_items_orphan_idx
    on support.faq_items (sort_order, id)
    where is_active and type_id is null;
