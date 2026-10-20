-- Фото в обращениях: Telegram file_id позволяет показать и переслать файл
-- без хранения копии изображения на сервере.
alter table support.message_history
    add column photo_file_id text;
