#!/usr/bin/env python3
"""Экспорт диалога omp в текст — чтобы отдать его другому чату или модели.

    scripts/export-chat.py                    # новейший диалог → ./chat-export.md
    scripts/export-chat.py --format txt       # простым текстом, без markdown
    scripts/export-chat.py --with-tools       # оставить также вызовы инструментов
    scripts/export-chat.py --with-thinking    # оставить блоки размышлений
    scripts/export-chat.py <id|путь> -o out.md

Что попадает в файл по умолчанию: реплики человека и ответы ассистента,
плюс сводки сжатий (в них — постановка задачи и состояние работы, без них
диалог читается обрывками). Что НЕ попадает: вывод инструментов, блоки
размышлений и результаты поиска по коду — это ~90% объёма и для внешнего
читателя мусор.

Секреты вычищаются всегда: токен бота и пароль из DATABASE_URL заменяются
на плейсхолдеры. Диалог про бота по определению содержит и то и другое.
"""

from __future__ import annotations

import argparse
import datetime as dt
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from _session import (
    first_user_text,
    message_text,
    message_thinking,
    message_tool_calls,
    newest_source,
    read_session,
    resolve_source,
)

# Токен бота BotFather: цифры, двоеточие, длинная base64-строка.
BOT_TOKEN = re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{30,}\b")
# Пароль внутри строки подключения: scheme://user:PASSWORD@host
DSN_PASSWORD = re.compile(r"(://[^:/@\s]+:)([^@/\s]+)(@)")


def redact(text: str) -> str:
    """Заменить секреты плейсхолдерами.

    Диалог про бота обязательно содержит токен (его подставляли в .env) и
    пароль базы (он в DATABASE_URL), поэтому вычистка — не опция, а часть
    экспорта: файл для внешнего сервиса.
    """
    text = BOT_TOKEN.sub("<BOT_TOKEN>", text)
    return DSN_PASSWORD.sub(r"\1<PASSWORD>\3", text)


# Метка роли. Именно `=====`, а не `#`: ответы ассистента полны markdown-
# заголовков (он пишет этой же разметкой документацию), и `### Ассистент`
# в них неотличим от настоящей метки роли. `=====` в обычном тексте не
# встречается, поэтому граница реплики читается однозначно.
RULE = "=" * 72


def fmt_time(iso: str) -> str:
    try:
        moment = dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return iso or "?"
    return moment.astimezone().strftime("%d.%m %H:%M")


def build(
    header: dict,
    body: list[dict],
    *,
    with_tools: bool,
    with_thinking: bool,
) -> tuple[str, dict[str, int]]:
    """Собрать текст диалога. Возвращает текст и счётчики отброшенного."""
    stats = {"user": 0, "assistant": 0, "tool_calls": 0, "tool_results": 0, "compactions": 0}
    out: list[str] = []

    for entry in body:
        kind = entry.get("type")

        if kind == "compaction":
            # Сводка сжатия — не мусор: там постановка задачи и что уже
            # сделано. Без неё экспорт читается обрывками.
            stats["compactions"] += 1
            summary = entry.get("summary") or entry.get("shortSummary") or ""
            if summary:
                out.append(f"{RULE}\n[сводка предыдущей части]\n\n{redact(summary.strip())}\n")

        elif kind == "custom_message":
            content = entry.get("content")
            text = content if isinstance(content, str) else ""
            if text.strip():
                out.append(f"{RULE}\n[системная вставка]\n\n{redact(text.strip())}\n")

        elif kind == "message":
            message = entry.get("message", {})
            role = message.get("role")
            stamp = fmt_time(entry.get("timestamp", ""))

            if role == "user":
                text = message_text(message)
                if text.strip():
                    stats["user"] += 1
                    out.append(f"{RULE}\nТЫ · {stamp}\n{RULE}\n\n{redact(text.strip())}\n")

            elif role == "assistant":
                text = message_text(message)
                thinking = message_thinking(message) if with_thinking else ""
                calls = message_tool_calls(message)
                if thinking.strip():
                    out.append(f"> **размышление**\n>\n> {redact(thinking.strip())}\n")
                if text.strip():
                    stats["assistant"] += 1
                    out.append(f"{RULE}\nАССИСТЕНТ · {stamp}\n{RULE}\n\n{redact(text.strip())}\n")
                if calls:
                    stats["tool_calls"] += len(calls)
                    if with_tools:
                        names = ", ".join(str(c.get("name", "?")) for c in calls)
                        out.append(f"*вызовы инструментов: {names}*\n")

            elif role == "toolResult":
                stats["tool_results"] += 1
                if with_tools:
                    content = message.get("content")
                    text = content if isinstance(content, str) else ""
                    if text.strip():
                        out.append(
                            f"### результат инструмента\n\n```\n{redact(text.strip())}\n```\n"
                        )

            elif role == "developer":
                text = message_text(message)
                if text.strip():
                    out.append(f"{RULE}\n[инструкция]\n\n{redact(text.strip())}\n")

    # Заголовок: явный → из сессии → имя каталога проекта. Первую реплику в
    # заголовок не ставим, хотя omp так и делает: в списке сессий это
    # помогает, а в файле для чтения получается простыня из трёхсот символов.
    project = pathlib.Path(header.get("cwd", "")).name or None
    title = header.get("title") or project or first_user_text(body) or "диалог"
    head = [
        f"# {title}",
        "",
        f"проект: `{header.get('cwd', '?')}`",
        f"диалог начат: {fmt_time(header.get('timestamp', ''))}",
        f"сессия: `{header.get('id', '?')}`",
        "",
        "---",
        "",
    ]
    return "\n".join(head + out), stats


def to_plain(text: str) -> str:
    """Markdown → простой текст: заголовки и жирный шрифт тут только мешают."""
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.M)
    text = text.replace("**", "").replace("`", "")
    text = re.sub(r"^>\s?", "", text, flags=re.M)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="export-chat.py",
        description="Экспорт диалога omp в текст для другого чата.",
    )
    parser.add_argument("source", nargs="?", help="путь к .jsonl или часть id")
    parser.add_argument("-o", "--out", type=pathlib.Path, default=pathlib.Path("chat-export.md"))
    parser.add_argument("--format", choices=["md", "txt"], default="md")
    parser.add_argument("--with-tools", action="store_true", help="включить вывод инструментов")
    parser.add_argument("--with-thinking", action="store_true", help="включить размышления")
    parser.add_argument(
        "--title",
        default=None,
        help="заголовок в шапке (по умолчанию — из сессии или первая реплика)",
    )
    args = parser.parse_args(argv)

    source = resolve_source(args.source) if args.source else newest_source(include_forks=True)
    header, body, broken = read_session(source)
    if args.title:
        header = {**header, "title": args.title}
    text, stats = build(
        header,
        body,
        with_tools=args.with_tools,
        with_thinking=args.with_thinking,
    )
    if args.format == "txt":
        text = to_plain(text)

    args.out.write_text(text + "\n", encoding="utf-8")
    size = args.out.stat().st_size

    print(f"источник:  {source.name}")
    print(f"записей:   {len(body)}" + (f" (оборванных строк: {broken})" if broken else ""))
    print(
        "вошло:     "
        f"реплик человека {stats['user']}, ответов {stats['assistant']}, "
        f"сводок {stats['compactions']}"
    )
    if not args.with_tools:
        print(
            f"вырезано:  результатов инструментов {stats['tool_results']}, "
            f"вызовов {stats['tool_calls']}"
        )
    if not args.with_thinking:
        print("вырезано:  блоки размышлений")
    secrets_found = "<BOT_TOKEN>" in text or "<PASSWORD>" in text
    print(f"секреты:   {'вычищены' if secrets_found else 'не найдены'}")
    print(f"\nготово: {args.out}  ({size / 1024:.0f} КБ)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
