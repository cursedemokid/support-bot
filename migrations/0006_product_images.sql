-- Галерея изображений в карточке товара. Один товар показывает не более трёх.

create table support.product_images (
    id bigserial primary key,
    product_id bigint not null references support.products(id) on delete cascade,
    file_id text not null check (length(file_id) between 1 and 512),
    sort_order integer not null check (sort_order between 0 and 2),
    unique (product_id, sort_order)
);

create index product_images_product_idx
    on support.product_images (product_id, sort_order);

-- Примеры для проверки клиентской карточки: названия и инструкции
-- демонстрационные, изображения взяты из предоставленных референсов.
insert into support.products (sku, title, description, pdf_file_id) values
    (
        'QW-V16-01',
        'DEMO BRAND V16 Flex',
        'Вертикальный пылесос для ежедневной уборки. Поворотная насадка помогает убирать вдоль мебели и в углах, а прозрачный контейнер легко контролировать и очищать.',
        'asset://instructions/demo-brand-v16-flex-demo.pdf'
    ),
    (
        'QW-V16-KIT',
        'DEMO BRAND V16 Care Kit',
        'Набор насадок для уборки мягкой мебели, узких зазоров и деликатных поверхностей. Подходит для расширения возможностей пылесоса DEMO BRAND V16.',
        'asset://instructions/demo-brand-v16-kit-demo.pdf'
    ),
    (
        'QW-SG01',
        'DEMO BRAND SteamGo S1',
        'Компактный ручной отпариватель для одежды и текстиля. Удобен для вертикального ухода за вещами и быстрого освежения ткани перед выходом.',
        'asset://instructions/demo-brand-steamgo-s1-demo.pdf'
    ),
    (
        'QW-AD02',
        'DEMO BRAND Aqua Duo',
        'Робот для влажной уборки с двумя вращающимися салфетками. Компактный корпус подойдёт для гладких напольных покрытий, плитки и повседневного поддержания чистоты.',
        'asset://instructions/demo-brand-aqua-duo-demo.pdf'
    )
on conflict (sku) do nothing;

insert into support.product_images (product_id, file_id, sort_order)
select product.id, image.file_id, image.sort_order
from support.products as product
join (
    values
        ('QW-V16-01', 'asset://products/demo-brand-v16-flex-1.png', 0),
        ('QW-V16-01', 'asset://products/demo-brand-v16-flex-2.png', 1),
        ('QW-V16-KIT', 'asset://products/demo-brand-v16-kit-1.png', 0),
        ('QW-SG01', 'asset://products/demo-brand-steamgo-s1-1.png', 0),
        ('QW-SG01', 'asset://products/demo-brand-steamgo-s1-2.png', 1),
        ('QW-AD02', 'asset://products/demo-brand-aqua-duo-1.png', 0),
        ('QW-AD02', 'asset://products/demo-brand-aqua-duo-2.png', 1)
) as image(sku, file_id, sort_order) on image.sku = product.sku
on conflict (product_id, sort_order) do nothing;
