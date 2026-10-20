#!/usr/bin/env python3
"""Перенести текущий диалог omp в историю проекта как форк.

Зачем: сессии omp лежат в бакете по каталогу запуска
(`~/.omp/agent/sessions/<encoded-cwd>/`), а пикер показывает сначала только
текущую папку. Диалог, начатый из `/tmp`, в проекте `/tmp/support-bot` не
виден — его надо положить в бакет проекта.

Скрипт делает то же, что `omp --fork <id>`, но без лишнего хода модели:
копирует записи диалога как есть и пишет новый заголовок с `cwd` проекта.
Форк — снимок: то, что сказано после него, в нём не появится. Повторный
запуск пересобирает снимок (старые форки этого источника удаляются).

    scripts/sync-session.py                     # новейший диалог из /tmp → в проект
    scripts/sync-session.py <путь-или-id>       # конкретный диалог
    scripts/sync-session.py --list              # что вообще есть

Дальше в VSCode, открытом на каталоге проекта: `omp --resume` и выбрать
диалог по названию, либо `omp --resume <id>`.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from _session import (
    SESSIONS,
    encode_cwd,
    first_user_text,
    newest_source,
    read_session,
    resolve_source,
    title_slot,
    uuid7,
)


def drop_previous_forks(
    target_dir: pathlib.Path, source_id: str, source_body: list[dict]
) -> tuple[int, list[str]]:
    """Убрать прежние снимки этого же источника, если в них не работали.

    «Не работали» проверяется строго: тело снимка обязано быть началом
    текущего тела источника. Если в снимке есть что-то сверх — значит,
    человек его открыл и продолжил, и удалять это нельзя: молча стереть
    чужую работу хуже, чем оставить лишний файл в списке.
    """
    removed = 0
    kept: list[str] = []
    for existing in target_dir.glob("*.jsonl"):
        try:
            header, body, _ = read_session(existing)
        except (ValueError, OSError):
            continue
        if header.get("parentSession") != source_id:
            continue
        untouched = len(body) <= len(source_body) and body == source_body[: len(body)]
        if untouched:
            existing.unlink()
            removed += 1
        else:
            kept.append(existing.name)
    return removed, kept


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sync-session.py",
        description="Положить диалог omp в историю проекта (форк-снимок).",
    )
    parser.add_argument("source", nargs="?", help="путь к .jsonl или часть id")
    parser.add_argument("--into", type=pathlib.Path, default=pathlib.Path.cwd())
    parser.add_argument("--title", default=None)
    parser.add_argument("--list", action="store_true", help="показать доступные сессии")
    args = parser.parse_args(argv)

    if args.list:
        files = sorted(SESSIONS.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        for f in files:
            try:
                header, body, _ = read_session(f)
            except (ValueError, OSError) as exc:
                print(f"{f}\n  пропущен: {exc}")
                continue
            print(f"{f.name}\n  cwd={header.get('cwd')}  записей={len(body)}")
        return 0

    source = resolve_source(args.source) if args.source else newest_source()
    source_header, body, broken = read_session(source)
    if not body:
        print(f"{source.name}: диалог пуст, переносить нечего", file=sys.stderr)
        return 1

    target_dir = SESSIONS / encode_cwd(args.into)
    target_dir.mkdir(parents=True, exist_ok=True)
    removed, kept = drop_previous_forks(target_dir, source_header["id"], body)

    title = args.title or first_user_text(body) or f"Диалог {args.into.name}"
    now_ms = int(time.time() * 1000)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + f".{now_ms % 1000:03d}Z"
    session_id = uuid7()
    dest = target_dir / f"{stamp.replace(':', '-').replace('.', '-')}_{session_id}.jsonl"

    header = {
        "type": "session",
        "version": 3,
        "id": session_id,
        "timestamp": stamp,
        "cwd": str(args.into.resolve()),
        "parentSession": source_header["id"],
        "providerPromptCacheKey": source_header["id"],
    }
    with dest.open("w", encoding="utf-8") as fh:
        fh.write(title_slot(title, stamp).decode("utf-8") + "\n")
        fh.write(json.dumps(header, ensure_ascii=False, separators=(",", ":")) + "\n")
        for entry in body:
            fh.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")

    print(f"источник: {source}")
    print(f"записей перенесено: {len(body)}" + (f" (оборванных строк: {broken})" if broken else ""))
    if removed:
        print(f"прежних снимков удалено: {removed}")
    if kept:
        print(f"оставлены снимки с продолженной работой ({len(kept)}): " + ", ".join(kept))
    print(f"создан: {dest}")
    print(f"\nоткрыть в VSCode на {args.into}:  omp --resume {session_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
