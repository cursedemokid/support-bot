"""Общее для скриптов, работающих с историей сессий omp.

Формат описан в `omp://session.md`: файлы лежат в бакете по каталогу запуска,
текущие начинаются со слота заголовка фиксированной ширины, затем заголовок
сессии и записи дерева.
"""

from __future__ import annotations

import json
import os
import pathlib
import secrets
import time
import uuid

SESSIONS = pathlib.Path.home() / ".omp" / "agent" / "sessions"
TITLE_SLOT_BYTES = 256


def encode_cwd(path: pathlib.Path) -> str:
    """Каталог → имя бакета, ровно как в omp (см. omp://session.md).

    `-<relative>` под домашним каталогом, `-tmp-<relative>` под временным
    корнем, `--<absolute>--` для всего остального; разделители → `-`.
    """
    resolved = path.resolve()
    home = pathlib.Path.home().resolve()
    temp = pathlib.Path(os.environ.get("TMPDIR", "/tmp")).resolve()
    for root, prefix in ((home, "-"), (temp, "-tmp-")):
        try:
            rel = resolved.relative_to(root)
        except ValueError:
            continue
        parts = [*rel.parts]
        # Сам корень (/tmp, ~) — бакет без относительной части.
        if not parts:
            return prefix.rstrip("-") or prefix
        return prefix + "-".join(parts)
    return "--" + str(resolved).replace("/", "-") + "--"


def read_session(path: pathlib.Path) -> tuple[dict, list[dict], int]:
    """Заголовок и записи. Неполную последнюю строку отбрасывает.

    Живая сессия дописывается прямо сейчас, поэтому последняя строка может
    быть оборвана — это нормально, теряется максимум один незавершённый ход.

    Текущие файлы начинаются со слота заголовка, старые — сразу с заголовка
    сессии; принимаем оба вида.
    """
    entries: list[dict] = []
    broken = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            broken += 1

    if not entries:
        msg = f"{path}: пустой файл сессии"
        raise ValueError(msg)
    if entries[0].get("type") == "session":
        header, body = entries[0], entries[1:]
    elif entries[0].get("type") == "title" and len(entries) > 1:
        header, body = entries[1], entries[2:]
    else:
        msg = f"{path}: не похоже на файл сессии omp"
        raise ValueError(msg)
    if header.get("type") != "session" or not header.get("id"):
        msg = f"{path}: нет заголовка сессии"
        raise ValueError(msg)
    return header, body, broken


def is_fork(path: pathlib.Path) -> bool:
    """Файл — снимок, а не самостоятельный диалог (есть parentSession)."""
    try:
        header, _, _ = read_session(path)
    except (ValueError, OSError):
        return False
    return bool(header.get("parentSession"))


def newest_source(include_forks: bool = False) -> pathlib.Path:
    """Новейший диалог из всех бакетов.

    Именно новейший, а не из текущего каталога: скрипт запускают из проекта,
    а диалог, который надо забрать, идёт в другом каталоге и пишется прямо
    сейчас — то есть он и есть самый свежий по mtime.

    `include_forks=False` исключает снимки (файлы с `parentSession`): это
    нужно `sync-session.py`, где снимок, сделанный прошлым запуском, свежее
    источника, и без правила второй запуск взял бы источником собственный
    результат.

    Для экспорта всё наоборот: если человек продолжил работу в снимке, живой
    диалог — это именно снимок, и берётся он (`include_forks=True`).
    """
    files = [f for f in SESSIONS.rglob("*.jsonl") if f.stat().st_size > 0]
    if not files:
        msg = f"в {SESSIONS} нет сессий — укажите источник явно"
        raise SystemExit(msg)
    pool = files if include_forks else ([f for f in files if not is_fork(f)] or files)
    return max(pool, key=lambda f: f.stat().st_mtime)


def resolve_source(value: str) -> pathlib.Path:
    path = pathlib.Path(value)
    if path.exists():
        return path
    matches = [f for f in SESSIONS.rglob("*.jsonl") if value.lower() in f.name.lower()]
    if not matches:
        msg = f"сессия {value!r} не найдена"
        raise SystemExit(msg)
    return max(matches, key=lambda f: f.stat().st_mtime)


def uuid7() -> str:
    """UUIDv7 — тот же вид time-ordered id, что генерирует omp."""
    ms = int(time.time() * 1000)
    raw = bytearray(ms.to_bytes(6, "big") + secrets.token_bytes(10))
    raw[6] = (raw[6] & 0x0F) | 0x70
    raw[8] = (raw[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(raw)))


def title_slot(title: str, updated: str) -> bytes:
    """Первая строка файла — слот заголовка фиксированной ширины 256 байт."""
    head = json.dumps(
        {"type": "title", "v": 1, "title": title, "updatedAt": updated},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    base = head[:-1] + ',"pad":"'
    pad = TITLE_SLOT_BYTES - len((base + '"}').encode())
    if pad < 0:
        msg = f"заголовок не влезает в слот: {title!r}"
        raise ValueError(msg)
    return (base + " " * pad + '"}').encode()


def first_user_text(body: list[dict], limit: int = 70) -> str | None:
    """Первое сообщение человека — из него делается заголовок по умолчанию."""
    for entry in body:
        if entry.get("type") != "message":
            continue
        message = entry.get("message", {})
        if message.get("role") != "user":
            continue
        text = message_text(message)
        if text:
            flat = " ".join(text.split())
            return flat[:limit] + ("…" if len(flat) > limit else "")
    return None


def message_text(message: dict) -> str:
    """Текст сообщения. У assistant в content блоки, у user бывает строка."""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
    return "\n".join(p for p in parts if p)


def message_thinking(message: dict) -> str:
    """Блоки размышления — отдельно от ответа, они не часть реплики."""
    content = message.get("content")
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "thinking":
            text = block.get("thinking", "")
            if text:
                parts.append(text)
    return "\n".join(parts)


def message_tool_calls(message: dict) -> list[dict]:
    content = message.get("content")
    if not isinstance(content, list):
        return []
    return [b for b in content if isinstance(b, dict) and b.get("type") == "toolCall"]
