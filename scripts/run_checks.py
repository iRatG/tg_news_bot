#!/usr/bin/env python3
"""
Pre-deploy / pre-commit проверки — scripts/run_checks.py.

Запуск:
    python scripts/run_checks.py           # локально
    python scripts/run_checks.py --fast    # пропустить API-вызовы (Perplexity, arXiv)

    # В Docker (та же среда, что на VPS):
    docker run --rm -v %cd%/data:/app/data --env-file .env newsbot:latest python scripts/run_checks.py

Блоки проверок:
    [ENV]       Все обязательные переменные окружения заданы
    [DB]        Подключение к БД, таблицы, источники, слоты расписания
    [TELEGRAM]  Bot token валиден (getMe)
    [PERPLEXITY] API доступен, пинг модели sonar
    [RSS]       feedparser на активных источниках, socket timeout
    [ARXIV]     httpx к export.arxiv.org, парсинг Atom XML
    [UNITS]     _validate_html, tier-scoring, brand-detection (без сети)

Выход:
    0 — все блоки прошли (можно коммитить и деплоить)
    1 — есть провальные блоки
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Сначала загружаем .env (локальный запуск) — реальные значения в приоритете
_env_file = PROJECT_ROOT / ".env"
if _env_file.exists():
    try:
        from dotenv import dotenv_values
        for k, v in dotenv_values(_env_file).items():
            if k not in os.environ:
                os.environ[k] = v
    except ImportError:
        pass

# Потом — дефолты-заглушки чтобы модули могли импортироваться без .env
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/newsbot.db")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "0:test")
os.environ.setdefault("TELEGRAM_CHANNEL_ID", "@test")
os.environ.setdefault("TELEGRAM_ADMIN_CHAT_ID", "0")
os.environ.setdefault("ADMIN_USERNAME", "admin")
os.environ.setdefault("ADMIN_PASSWORD", "test")

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

# ── CLI флаги ─────────────────────────────────────────────────────────────────
FAST_MODE = "--fast" in sys.argv  # пропускает Perplexity и arXiv (долгие/дорогие)

# ── Состояние блоков ──────────────────────────────────────────────────────────
_blocks: list[dict] = []   # {name, checks: [{test, status, detail}]}
_cur_block: dict | None = None

OK   = "✓"
FAIL = "✗"
SKIP = "·"
WARN = "!"


def block(name: str) -> None:
    global _cur_block
    _cur_block = {"name": name, "checks": []}
    _blocks.append(_cur_block)
    print(f"\n[{name}]")


def check(test: str, status: str, detail: str = "") -> None:
    """status: ok | fail | skip | warn"""
    icon = {"ok": OK, "fail": FAIL, "skip": SKIP, "warn": WARN}.get(status, "?")
    line = f"  [{icon}] {test}"
    if detail:
        line += f" — {detail}"
    print(line)
    if _cur_block is not None:
        _cur_block["checks"].append({"test": test, "status": status, "detail": detail})


def _block_status(b: dict) -> str:
    statuses = [c["status"] for c in b["checks"]]
    if "fail" in statuses:
        return "FAIL"
    if "warn" in statuses:
        return "WARN"
    if all(s == "skip" for s in statuses):
        return "SKIP"
    return "PASS"


# ══════════════════════════════════════════════════════════════════════════════
# BLOCK 1 — ENV
# ══════════════════════════════════════════════════════════════════════════════

def check_env() -> None:
    block("ENV  — переменные окружения")

    required = {
        "PERPLEXITY_API_KEY":    lambda v: v.startswith("pplx-"),
        "TELEGRAM_BOT_TOKEN":    lambda v: ":" in v and not v.startswith("0:"),
        "TELEGRAM_CHANNEL_ID":   lambda v: bool(v),
        "TELEGRAM_ADMIN_CHAT_ID": lambda v: v.isdigit() and v != "0",
        "DATABASE_URL":          lambda v: "sqlite" in v or "postgres" in v,
        "ADMIN_PASSWORD":        lambda v: len(v) >= 6,
    }

    for key, validate in required.items():
        val = os.environ.get(key, "")
        if not val:
            check(key, "fail", "не задан")
        elif not validate(val):
            check(key, "warn", f"задан, но выглядит неправильно: {val[:20]}…")
        else:
            check(key, "ok", "OK")


# ══════════════════════════════════════════════════════════════════════════════
# BLOCK 2 — DB
# ══════════════════════════════════════════════════════════════════════════════

REQUIRED_TABLES = [
    "pipeline_runs", "raw_articles", "published_posts",
    "sources", "agent_logs", "settings",
    "schedule_slots", "arxiv_seen_papers", "channel_stats_history",
]


async def check_db() -> None:
    block("DB   — база данных")

    try:
        from db.database import async_session_factory
        from sqlalchemy import text

        async with async_session_factory() as session:

            # Таблицы
            rows = (await session.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            )).fetchall()
            existing = {r[0] for r in rows}
            missing = [t for t in REQUIRED_TABLES if t not in existing]
            if missing:
                check("таблицы", "fail", f"отсутствуют: {', '.join(missing)}")
            else:
                check("таблицы", "ok", f"{len(REQUIRED_TABLES)}/{len(REQUIRED_TABLES)} таблиц")

            # Источники
            total = (await session.execute(text("SELECT COUNT(*) FROM sources"))).scalar()
            active = (await session.execute(
                text("SELECT COUNT(*) FROM sources WHERE is_active=1")
            )).scalar()
            if active == 0:
                check("источники", "fail", "нет активных источников")
            elif active < 5:
                check("источники", "warn", f"{active}/{total} активных (мало)")
            else:
                check("источники", "ok", f"{active}/{total} активных")

            # Слоты расписания
            slots = (await session.execute(
                text("SELECT hour, minute FROM schedule_slots WHERE is_active=1 ORDER BY hour")
            )).fetchall()
            if not slots:
                check("слоты расписания", "warn", "нет активных слотов — бот не будет публиковать")
            else:
                times = ", ".join(f"{h:02d}:{m:02d}" for h, m in slots)
                check("слоты расписания", "ok", f"{len(slots)} слота: {times}")

            # Ключевые настройки
            keys = ("morning_digest_enabled", "morning_digest_hour",
                    "arxiv_schedule_enabled", "arxiv_schedule_hour", "arxiv_max_papers")
            settings_rows = (await session.execute(
                text(f"SELECT key,value FROM settings WHERE key IN {keys!r}")
            )).fetchall()
            settings = dict(settings_rows)
            missing_s = [k for k in keys if k not in settings]
            if missing_s:
                check("settings", "warn", f"отсутствуют: {', '.join(missing_s)}")
            else:
                digest_h = settings.get("morning_digest_hour", "?")
                arxiv_h  = settings.get("arxiv_schedule_hour", "?")
                check("settings", "ok",
                      f"digest={digest_h}:00 | arxiv={arxiv_h}:00")

    except Exception as exc:
        check("подключение", "fail", str(exc))


# ══════════════════════════════════════════════════════════════════════════════
# BLOCK 3 — TELEGRAM
# ══════════════════════════════════════════════════════════════════════════════

async def check_telegram() -> None:
    block("TG   — Telegram Bot API")

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token or token.startswith("0:"):
        check("getMe", "skip", "нет токена — пропущено")
        return

    try:
        from telegram import Bot
        t0 = time.monotonic()
        async with Bot(token=token) as bot:
            me = await bot.get_me()
        elapsed = int((time.monotonic() - t0) * 1000)
        check("getMe", "ok", f"@{me.username} (id={me.id}) | {elapsed}мс")
    except Exception as exc:
        check("getMe", "fail", str(exc))


# ══════════════════════════════════════════════════════════════════════════════
# BLOCK 4 — PERPLEXITY
# ══════════════════════════════════════════════════════════════════════════════

async def check_perplexity() -> None:
    block("PPX  — Perplexity API")

    if FAST_MODE:
        check("ping", "skip", "--fast режим")
        return

    api_key = os.environ.get("PERPLEXITY_API_KEY", "")
    if not api_key or not api_key.startswith("pplx-"):
        check("ping", "skip", "нет PERPLEXITY_API_KEY")
        return

    try:
        import openai
        client = openai.AsyncOpenAI(
            api_key=api_key,
            base_url="https://api.perplexity.ai",
        )
        t0 = time.monotonic()
        resp = await client.chat.completions.create(
            model="sonar",
            messages=[{"role": "user", "content": "Say: OK"}],
            max_tokens=5,
        )
        elapsed = int((time.monotonic() - t0) * 1000)
        model = resp.model
        in_t  = resp.usage.prompt_tokens if resp.usage else "?"
        out_t = resp.usage.completion_tokens if resp.usage else "?"
        check("ping", "ok", f"model={model} | {in_t}in+{out_t}out | {elapsed}мс")
    except Exception as exc:
        check("ping", "fail", str(exc))


# ══════════════════════════════════════════════════════════════════════════════
# BLOCK 5 — RSS
# ══════════════════════════════════════════════════════════════════════════════

async def check_rss() -> None:
    block("RSS  — источники feedparser")

    try:
        import socket
        import feedparser
    except ImportError as e:
        check("import", "fail", str(e))
        return

    # Берём активные источники из БД (первые 5)
    urls: list[tuple[str, str]] = []
    try:
        from db.database import async_session_factory
        from sqlalchemy import text
        async with async_session_factory() as session:
            rows = (await session.execute(
                text("SELECT name, url FROM sources WHERE is_active=1 LIMIT 5")
            )).fetchall()
        urls = [(r[0], r[1]) for r in rows]
    except Exception:
        pass

    if not urls:
        urls = [
            ("HuggingFace Blog",  "https://huggingface.co/blog/feed.xml"),
            ("Simon Willison",    "https://simonwillison.net/atom/everything/"),
        ]

    ok_count   = 0
    fail_count = 0
    total_articles = 0

    for name, url in urls:
        t0 = time.monotonic()
        try:
            socket.setdefaulttimeout(10)
            feed = feedparser.parse(url, request_headers={
                "User-Agent": "Mozilla/5.0",
                "Accept": "application/rss+xml,application/atom+xml",
            })
            elapsed = int((time.monotonic() - t0) * 1000)
            n = len(feed.entries)
            if n > 0:
                bozo_note = " (malformed XML)" if feed.bozo else ""
                check(name[:35], "ok", f"{n} статей | {elapsed}мс{bozo_note}")
                ok_count += 1
                total_articles += n
            else:
                status = "warn" if feed.bozo else "skip"
                check(name[:35], status, f"0 статей | {elapsed}мс")
                fail_count += 1
        except Exception as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            check(name[:35], "fail", f"{elapsed}мс: {type(exc).__name__}")
            fail_count += 1

    if ok_count == 0:
        check("итог", "fail", "ни один источник не ответил")
    else:
        check("итог", "ok",
              f"{ok_count}/{len(urls)} источников, ~{total_articles} статей суммарно")


# ══════════════════════════════════════════════════════════════════════════════
# BLOCK 6 — ARXIV
# ══════════════════════════════════════════════════════════════════════════════

async def check_arxiv() -> None:
    block("ARXIV — arXiv API (httpx)")

    if FAST_MODE:
        check("fetch", "skip", "--fast режим")
        return

    try:
        import httpx
        import feedparser as fp

        ARXIV_URL = "https://export.arxiv.org/api/query"
        params = {
            "search_query": "ti:language+model+OR+ti:LLM",
            "max_results": 3,
            "sortBy": "submittedDate",
        }
        timeout = httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=5.0)

        t0 = time.monotonic()
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.get(ARXIV_URL, params=params)
        elapsed = int((time.monotonic() - t0) * 1000)

        if resp.status_code != 200:
            check("HTTP", "fail", f"status={resp.status_code}")
            return

        feed = fp.parse(resp.text)
        n = len(feed.entries)
        if n == 0:
            check("fetch", "warn", f"0 результатов | {elapsed}мс")
        else:
            first = feed.entries[0].get("title", "?")[:50]
            check("fetch", "ok", f"{n} статей | {elapsed}мс | «{first}…»")
    except Exception as exc:
        check("fetch", "fail", str(exc))


# ══════════════════════════════════════════════════════════════════════════════
# BLOCK 7 — UNIT TESTS (без сети, без API)
# ══════════════════════════════════════════════════════════════════════════════

def check_units() -> None:
    block("UNIT — unit-тесты (без сети)")

    # 1. _validate_html: <br> → \n
    try:
        from agents.formatter import _validate_html
        result = _validate_html("<br>hello<br>")
        assert "\n" in result and "<br>" not in result
        check("formatter: <br> → \\n", "ok")
    except Exception as exc:
        check("formatter: <br> → \\n", "fail", str(exc))

    # 2. _validate_html: неподдерживаемые теги удаляются
    try:
        from agents.formatter import _validate_html
        result = _validate_html("<p>text</p><div>block</div>")
        assert "<p>" not in result and "<div>" not in result
        check("formatter: <p>/<div> удалены", "ok")
    except Exception as exc:
        check("formatter: <p>/<div> удалены", "fail", str(exc))

    # 3. _validate_html: разрешённые теги остаются
    try:
        from agents.formatter import _validate_html
        html = '<b>bold</b> <i>italic</i> <a href="http://x.com">link</a>'
        result = _validate_html(html)
        assert "<b>" in result and "<i>" in result and "<a " in result
        check("formatter: <b>/<i>/<a> сохранены", "ok")
    except Exception as exc:
        check("formatter: <b>/<i>/<a> сохранены", "fail", str(exc))

    # 4. _detect_tier: breakthrough при ключевом слове
    try:
        from agents.researcher import _detect_tier
        tier = _detect_tier("New LLM Benchmark Results", "")
        assert tier == "breakthrough", f"ожидался breakthrough, получен {tier}"
        check("researcher: tier=breakthrough (benchmark)", "ok")
    except Exception as exc:
        check("researcher: tier=breakthrough (benchmark)", "fail", str(exc))

    # 5. _detect_tier: noise при ключевом слове
    try:
        from agents.researcher import _detect_tier
        tier = _detect_tier("Series B Funding Round for AI startup", "")
        assert tier == "noise", f"ожидался noise, получен {tier}"
        check("researcher: tier=noise (funding)", "ok")
    except Exception as exc:
        check("researcher: tier=noise (funding)", "fail", str(exc))

    # 6. _detect_brand: OpenAI
    try:
        from agents.researcher import _detect_brand
        brand = _detect_brand("OpenAI releases new model", "", "https://openai.com/blog")
        assert brand == "openai", f"ожидался openai, получен {brand}"
        check("researcher: brand=openai", "ok")
    except Exception as exc:
        check("researcher: brand=openai", "fail", str(exc))

    # 7. _compute_diversity_mult: новый бренд → максимальный множитель
    try:
        from agents.researcher import _compute_diversity_mult
        mult = _compute_diversity_mult("mistral", {"openai": 10, "google": 5})
        assert mult == 2.0, f"ожидался 2.0, получен {mult}"
        check("researcher: diversity_mult=2.0 (новый бренд)", "ok")
    except Exception as exc:
        check("researcher: diversity_mult=2.0 (новый бренд)", "fail", str(exc))

    # 8. Config: импорт settings
    try:
        from core.config import settings
        assert settings.DATABASE_URL
        check("config: settings загружается", "ok")
    except Exception as exc:
        check("config: settings загружается", "fail", str(exc))


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

async def main() -> None:
    print("=" * 60)
    print("  NewsBot — Pre-deploy Checks")
    if FAST_MODE:
        print("  (fast mode: Perplexity и arXiv пропущены)")
    print("=" * 60)

    t_start = time.monotonic()

    # Синхронные блоки
    check_env()
    check_units()

    # Асинхронные блоки
    await check_db()
    await check_telegram()
    await check_perplexity()
    await check_rss()
    await check_arxiv()

    elapsed_total = int((time.monotonic() - t_start) * 1000)

    # Итог
    print("\n" + "=" * 60)
    results_summary = []
    all_pass = True
    for b in _blocks:
        status = _block_status(b)
        icon   = OK if status == "PASS" else (SKIP if status == "SKIP" else (WARN if status == "WARN" else FAIL))
        print(f"  [{icon}] {b['name']:<6} {status}")
        results_summary.append(status)
        if status == "FAIL":
            all_pass = False

    pass_count = results_summary.count("PASS")
    fail_count = results_summary.count("FAIL")
    warn_count = results_summary.count("WARN")

    print(f"\n  {pass_count}/{len(_blocks)} блоков прошли"
          + (f" | {fail_count} FAIL" if fail_count else "")
          + (f" | {warn_count} WARN" if warn_count else "")
          + f" | {elapsed_total / 1000:.1f}с")
    print("=" * 60)

    if all_pass:
        print("\n[OK] Все проверки пройдены — можно коммитить и деплоить\n")
        sys.exit(0)
    else:
        print("\n[!] Есть ошибки — исправь перед коммитом/деплоем\n")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
