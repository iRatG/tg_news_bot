#!/usr/bin/env python3
"""
Локальный тест ключевых агентов — scripts/test_local.py.

Запускать ПЕРЕД деплоем на VPS:
    # Вариант 1: локально (Python)
    python scripts/test_local.py

    # Вариант 2: в локальном Docker (самый надёжный — та же среда что на VPS)
    docker build -t newsbot:latest .
    docker run --rm -v %cd%/data:/app/data --env-file .env newsbot:latest python scripts/test_local.py

Что проверяет:
    [arxiv]   fetch_new_papers()  — HTTP к arxiv.org, парсинг XML, дедупликация
    [arxiv]   process_paper()     — суммаризация через Perplexity (если ключ есть)
    [rss]     researcher fetch    — feedparser на 2-3 источника, socket timeout
    [db]      подключение к БД    — async_session_factory, таблицы

Telegram НЕ используется. Публикации не происходит.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

# Добавляем корень проекта в sys.path — работает при запуске из любой папки
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# ── Настройка окружения ───────────────────────────────────────────────────────

# Можно запускать без .env — подставим минимальный DATABASE_URL
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/newsbot.db")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "0:test")
os.environ.setdefault("TELEGRAM_CHANNEL_ID", "@test")
os.environ.setdefault("TELEGRAM_ADMIN_CHAT_ID", "0")
os.environ.setdefault("ADMIN_USERNAME", "admin")
os.environ.setdefault("ADMIN_PASSWORD", "test")

# UTF-8 для Windows
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

OK   = "✓"
FAIL = "✗"
SKIP = "·"

results: list[tuple[str, str, str]] = []  # (test, status, detail)


def log(test: str, status: str, detail: str = "") -> None:
    icon = OK if status == "ok" else (SKIP if status == "skip" else FAIL)
    line = f"  [{icon}] {test}"
    if detail:
        line += f" — {detail}"
    print(line)
    results.append((test, status, detail))


# ── Тест 1: подключение к БД ──────────────────────────────────────────────────

async def test_db() -> None:
    print("\n[db] Подключение к БД")
    try:
        from db.database import async_session_factory
        from sqlalchemy import text
        async with async_session_factory() as session:
            row = (await session.execute(text("SELECT COUNT(*) FROM sources"))).scalar()
        log("sources count", "ok", f"{row} источников")
    except Exception as exc:
        log("sources count", "fail", str(exc))

    try:
        from sqlalchemy import text
        from db.database import async_session_factory
        async with async_session_factory() as session:
            row = (await session.execute(
                text("SELECT COUNT(*) FROM arxiv_seen_papers")
            )).scalar()
        log("arxiv_seen_papers", "ok", f"{row} записей")
    except Exception as exc:
        log("arxiv_seen_papers", "fail", f"таблица не найдена — нужна миграция: {exc}")


# ── Тест 2: arXiv fetch ───────────────────────────────────────────────────────

async def test_arxiv_fetch() -> None:
    print("\n[arxiv] fetch_new_papers()")
    try:
        from agents.arxiv_agent import ArxivAgent, ARXIV_QUERIES
        agent = ArxivAgent()

        t0 = time.monotonic()
        papers = await agent.fetch_new_papers()
        elapsed = int((time.monotonic() - t0) * 1000)

        if papers:
            log("fetch_new_papers", "ok",
                f"{len(papers)} новых бумаг за {elapsed}мс")
            for p in papers[:3]:
                log(f"  {p['arxiv_id']}", "ok",
                    f"{p['title'][:55]}...")
        else:
            log("fetch_new_papers", "skip",
                f"0 новых бумаг ({elapsed}мс) — возможно все уже виданы")

        return papers
    except Exception as exc:
        log("fetch_new_papers", "fail", str(exc))
        return []


# ── Тест 3: arXiv summarize (требует PERPLEXITY_API_KEY) ─────────────────────

async def test_arxiv_summarize(papers: list) -> None:
    print("\n[arxiv] process_paper() — суммаризация")

    api_key = os.environ.get("PERPLEXITY_API_KEY", "")
    if not api_key or api_key.startswith("pplx-") is False:
        log("_summarize_paper", "skip", "нет PERPLEXITY_API_KEY — пропущено")
        return

    if not papers:
        log("_summarize_paper", "skip", "нет бумаг от fetch_new_papers()")
        return

    try:
        from agents.arxiv_agent import ArxivAgent
        agent = ArxivAgent()
        paper = papers[0]

        t0 = time.monotonic()
        post_html, in_tok, out_tok = await agent.process_paper(paper)
        elapsed = int((time.monotonic() - t0) * 1000)

        preview = post_html[:120].replace("\n", " ")
        log("process_paper", "ok",
            f"{elapsed}мс | {in_tok}in+{out_tok}out токенов")
        log("  html preview", "ok", f"{preview}...")
    except Exception as exc:
        log("process_paper", "fail", str(exc))


# ── Тест 4: RSS researcher fetch (2 источника) ────────────────────────────────

async def test_rss_fetch() -> None:
    print("\n[rss] Researcher — feedparser (2 источника)")
    try:
        import socket
        import feedparser

        # Берём источники из БД (если есть), иначе fallback на 2 надёжных
        test_urls: list[tuple[str, str]] = []
        try:
            from db.database import async_session_factory
            from sqlalchemy import text
            async with async_session_factory() as session:
                rows = (await session.execute(
                    text("SELECT name, url FROM sources WHERE is_active=1 LIMIT 3")
                )).fetchall()
            test_urls = [(r[0], r[1]) for r in rows]
        except Exception:
            pass
        if not test_urls:
            test_urls = [
                ("HuggingFace Blog", "https://huggingface.co/blog/feed.xml"),
                ("Simon Willison", "https://simonwillison.net/atom/everything/"),
            ]

        for name, url in test_urls:
            t0 = time.monotonic()
            try:
                socket.setdefaulttimeout(10)
                feed = feedparser.parse(url, request_headers={
                    "User-Agent": "Mozilla/5.0",
                    "Accept": "application/rss+xml,application/atom+xml",
                })
                elapsed = int((time.monotonic() - t0) * 1000)
                count = len(feed.entries)
                if count > 0:
                    # Есть данные — ок. bozo = malformed XML (известная проблема для ряда источников)
                    suffix = " (malformed XML — известно)" if feed.bozo else ""
                    log(name, "ok", f"{count} записей за {elapsed}мс{suffix}")
                elif feed.bozo:
                    log(name, "skip", f"bozo, 0 записей — источник недоступен ({elapsed}мс)")
                else:
                    log(name, "skip", f"0 записей ({elapsed}мс)")
            except Exception as exc:
                elapsed = int((time.monotonic() - t0) * 1000)
                log(name, "fail", f"{elapsed}мс: {exc}")
    except Exception as exc:
        log("feedparser import", "fail", str(exc))


# ── Main ──────────────────────────────────────────────────────────────────────

async def main() -> None:
    print("=" * 60)
    print("  NewsBot — локальный тест агентов")
    print("=" * 60)

    await test_db()
    papers = await test_arxiv_fetch()
    await test_arxiv_summarize(papers)
    await test_rss_fetch()

    # Итог
    ok_count   = sum(1 for _, s, _ in results if s == "ok")
    fail_count = sum(1 for _, s, _ in results if s == "fail")
    skip_count = sum(1 for _, s, _ in results if s == "skip")

    print(f"\n{'=' * 60}")
    print(f"  Итого: {ok_count} ок / {fail_count} ошибок / {skip_count} пропущено")
    print("=" * 60)

    if fail_count > 0:
        print("\n[!] Есть ошибки — не деплоить на VPS до исправления")
        sys.exit(1)
    else:
        print("\n[OK] Всё прошло — можно деплоить")


if __name__ == "__main__":
    asyncio.run(main())
