#!/usr/bin/env python3
"""
Тест нового "голоса" канала — scripts/test_voice.py.

Что делает:
    Берёт 3 последних опубликованных новостных поста из БД,
    прогоняет через НОВЫЙ промпт с секцией "💡 На практике",
    показывает: [было] vs [стало].

    Отдельно: один arXiv-пост с секцией "🛠 Применимо если:".

Что НЕ делает:
    — не пишет в БД
    — не публикует в Telegram
    — не меняет writer.py или любой другой файл пайплайна

Запуск:
    # Локально (нужен .env с PERPLEXITY_API_KEY):
    python scripts/test_voice.py

    # В Docker (та же среда что на VPS):
    docker run --rm -v %cd%/data:/app/data --env-file .env newsbot:latest python scripts/test_voice.py

    # Только один пост (быстро):
    python scripts/test_voice.py --n 1
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import textwrap
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Загружаем .env
_env_file = PROJECT_ROOT / ".env"
if _env_file.exists():
    try:
        from dotenv import dotenv_values
        for k, v in dotenv_values(_env_file).items():
            if k not in os.environ:
                os.environ[k] = v
    except ImportError:
        pass

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/newsbot.db")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "0:test")
os.environ.setdefault("TELEGRAM_CHANNEL_ID", "@test")
os.environ.setdefault("TELEGRAM_ADMIN_CHAT_ID", "0")
os.environ.setdefault("ADMIN_USERNAME", "admin")
os.environ.setdefault("ADMIN_PASSWORD", "test")

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

N_POSTS = 4  # сколько постов тестировать (по умолчанию)
for arg in sys.argv[1:]:
    if arg.startswith("--n="):
        N_POSTS = int(arg.split("=")[1])
    elif arg == "--n" and sys.argv.index(arg) + 1 < len(sys.argv):
        N_POSTS = int(sys.argv[sys.argv.index(arg) + 1])

# ══════════════════════════════════════════════════════════════════════════════
# ПРОМПТЫ v3 — два формата по образцу @ai_machinelearning_big_data
#
# Вывод из анализа бенчмарка:
#   - НЕ 4 стиля, а 2 формата по весу новости
#   - BRIEF (✔️): 180-320 симв, только факты, без анализа — как новостное агентство
#   - ANALYSIS (📌): 600-900 симв, реальный разбор с секциями 🟡,
#     "🟡 Капля реализма" только если есть реальный нюанс
#   - Язык: русский везде, английский только для истинных терминов
# ══════════════════════════════════════════════════════════════════════════════

# Общие правила языка и форматирования
_CHANNEL_DNA = (
    # Язык
    "Ты — редактор Telegram-канала об AI и LLM. "
    "Язык: русский. Английский — только для терминов без русского аналога: "
    "LLM, RAG, API, GPU, inference, fine-tuning, embedding, токены, промпт, "
    "FP8, GGUF, ELO, benchmark, serverless, open-source. "
    "Всё остальное — по-русски: "
    "не 'pipeline' а 'цепочка'; не 'deploy' а 'развёртывание'; "
    "не 'setup' а 'настройка'; не 'run' а 'запуск'; не 'rollback' а 'откат'; "
    "не 'babysitting' а 'ручной контроль'; не 'shipping' а 'выпуск'; "
    "не 'observability' а 'наблюдаемость'; не 'workflow' а 'процесс'. "
    # Форматирование
    "Форматирование: только Telegram HTML (<b>, <i>, <a href=''>). "
    "Никаких **звёздочек**, никакого Markdown, никакого счётчика символов в тексте. "
    # Честность
    "СТРОГО: используй только факты из источника. "
    "Не добавляй статистику, исследования или числа которых нет в тексте."
)

# ── ФОРМАТ 1: БРИФ ────────────────────────────────────────────────────────────
# Для обычных новостей. Только факты, никакого анализа.
# Образец: "✔️ Anthropic запустила Claude Code Security.\n\nНовый инструмент..."

SYSTEM_BRIEF = (
    _CHANNEL_DNA + "\n"
    "Пишешь короткие новостные брифы — только факты из источника. "
    "Стиль: нейтральный, новостное агентство. "
    "Никакого анализа, никаких выводов, никаких призывов к действию."
)

def USER_BRIEF(title: str, content: str, source_name: str, url: str) -> str:
    """Бриф v4: заголовок в новостном стиле + 1-2 факта + ссылка. Цель: 150-280 симв."""
    return (
        "Напиши новостной бриф для Telegram.\n\n"
        "Структура:\n"
        "✔️ <b>Заголовок</b>\n"
        "1-2 предложения: ключевые факты\n"
        f"source.com (<a href=\"{url}\">{url}</a>)\n\n"
        "ПРАВИЛА:\n"
        "— ОБЯЗАТЕЛЬНО начинай с ✔️ <b>Заголовок</b> — иконка и жирный текст\n"
        "— Заголовок ПРИДУМАЙ САМА на русском: «[Кто] [что сделал]»\n"
        "  Примеры: «OpenAI выпустила X», «Яндекс сократил затраты на Y%»\n"
        f"  НЕ копируй оригинал «{title[:60]}» — переведи суть\n"
        "  НЕ призывы: «Используйте X», «Планируйте Y»\n"
        "— Длина основного текста: 150-250 символов (не считая ссылку)\n"
        "— Только факты из источника — никакой отсебятины\n"
        "— Без анализа, без выводов, без цитат [1][2][3]\n\n"
        f"Заголовок источника: {title}\n"
        f"Содержание: {content[:800]}\n"
        f"Источник: {source_name}\n"
        f"URL: {url}"
    )

# ── ФОРМАТ 2: АНАЛИТИКА ───────────────────────────────────────────────────────
# Для важных историй. Реальный разбор с секциями 🟡.
# "🟡 Капля реализма" — только если есть реальный нюанс из источника.
# Образец: 📌 посты с секциями 🟡 из @ai_machinelearning_big_data.

SYSTEM_ANALYSIS = (
    _CHANNEL_DNA + "\n"
    "Пишешь аналитические посты для разработчиков и AI-инженеров которые сами "
    "строят с LLM, RAG и inference каждый день. "
    "Задача: не пересказать новость, а раскрыть тему — что за этим стоит, "
    "как это работает, что меняется в реальной работе. "
    "Ты можешь использовать реальные факты из открытых источников чтобы раскрыть тему глубже — "
    "конкретные числа, сравнения, контекст. Главное: только реально проверяемые факты, "
    "не выдуманные. "
    "Стиль: без маркетинга, без восторгов — как умный коллега который разобрался. "
    "Секция '🟡 Капля реализма' — только если есть реальное ограничение или противоречие."
)

def USER_ANALYSIS(title: str, content: str, source_name: str, url: str) -> str:
    """Аналитика v6: расширенные секции, max_tokens=950. Цель: 1200-1800 симв."""
    return (
        "Напиши аналитический пост для Telegram. Следуй структуре точно.\n\n"
        "СТРУКТУРА — каждый блок на отдельной строке:\n\n"
        "📌 <b>Заголовок</b>\n"
        "1-2 предложения-лид: что произошло и почему это важно именно сейчас.\n\n"
        "🟡 Название секции (2-4 слова — суть факта)\n"
        "3-5 предложений: разверни тему — конкретные факты, цифры, детали из источника.\n"
        "Не ограничивайся поверхностью — объясни механизм, покажи масштаб.\n\n"
        "🟡 Что это значит\n"
        "3-5 предложений: как это меняет реальную работу разработчика или AI-инженера.\n"
        "Конкретные примеры: что теперь стоит попробовать, что устарело, "
        "как это влияет на архитектуру систем, выбор инструментов, стоимость.\n\n"
        "Если в теме есть реальное ограничение, противоречие или цена вопроса:\n"
        "🟡 Капля реализма\n"
        "2-3 предложения: честная оговорка — конкретная, с цифрами если есть.\n\n"
        f"🔗 <a href=\"{url}\">{source_name}</a>\n\n"
        "ПРАВИЛА:\n"
        f"— Заголовок ПРИДУМАЙ САМА на русском — НЕ копируй «{title[:60]}»\n"
        "— Весь текст — на русском, даже если оригинал на английском\n"
        "— Название 🟡 секции — отдельная строка, текст секции — следующая строка\n"
        "— Длина без ссылки: 1200-1800 символов — раскрой тему полностью\n"
        "— Только Telegram HTML: <b>, <i>, <a href=''>. Никаких **звёздочек**\n"
        "— Используй реальные числа и факты. Не выдумывай\n"
        f"— Ссылку в конце пиши ТОЧНО ТАК: 🔗 <a href=\"{url}\">{source_name}</a> — не меняй URL\n\n"
        f"Заголовок: {title}\n"
        f"Содержание: {content[:1500]}\n"
        f"Источник: {source_name}\n"
        f"URL: {url}"
    )

# Словарь форматов для main()
NEW_STYLE_PROMPTS = {
    "brief":    (SYSTEM_BRIEF,    None),   # (system, user_fn) — user_fn вызывается отдельно
    "analysis": (SYSTEM_ANALYSIS, None),
}


def _new_single_prompt(title: str, content: str, source_name: str, url: str) -> str:
    """Заглушка — не используется напрямую в v3, см. USER_BRIEF / USER_ANALYSIS."""
    return USER_BRIEF(title, content, source_name, url)


def _new_arxiv_prompt(
    title: str, abstract: str, authors: str, arxiv_id: str, categories: str
) -> str:
    """arXiv-промпт v2: hook из abstract, честный финал, Капля реализма если слабая работа."""
    return (
        "Напиши пост о научной статье для Telegram-канала об AI.\n\n"
        "Структура:\n"
        "1. 📄 <b>Заголовок на русском</b>\n"
        f"2. 👥 {authors} | 📂 {categories}\n"
        "3. 2-3 предложения: что исследовали, главный результат, конкретные цифры если есть\n"
        "4. 1 предложение: для каких задач актуально прямо сейчас — "
        "или честно: 'пока академия, в прод не завтра'\n"
        f"5. 🔗 <a href=\"https://arxiv.org/abs/{arxiv_id}\">arXiv:{arxiv_id}</a>\n\n"
        "Язык: русский. "
        "Длина: 450-600 символов (не считая строки с авторами и ссылкой). "
        "Только Telegram HTML. Без Markdown. Без цитат [1][2][3].\n\n"
        f"Название: {title}\n"
        f"Аннотация: {abstract[:1500]}"
    )

# ══════════════════════════════════════════════════════════════════════════════
# ВЫЗОВ PERPLEXITY (копия из writer.py — не импортируем чтобы не зависеть)
# ══════════════════════════════════════════════════════════════════════════════

async def _call_perplexity(system: str, user: str, max_tokens: int = 700, search_context: str = "low") -> tuple[str, int, int, float]:
    """Вызывает Perplexity sonar. Возвращает (text, in_tok, out_tok, latency_sec)."""
    import openai
    from core.config import settings

    client = openai.AsyncOpenAI(
        api_key=settings.PERPLEXITY_API_KEY,
        base_url="https://api.perplexity.ai",
    )
    t0 = time.monotonic()
    resp = await client.chat.completions.create(
        model="sonar",
        messages=[
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
        temperature=0.3,  # ниже = меньше выдуманных фактов
        max_tokens=max_tokens,
        extra_body={"web_search_options": {"search_context_size": search_context}},
    )
    elapsed = time.monotonic() - t0
    text = resp.choices[0].message.content.strip()
    text = re.sub(r'\[\d+\]', '', text).strip()
    in_tok  = getattr(resp.usage, "prompt_tokens", 0)
    out_tok = getattr(resp.usage, "completion_tokens", 0)
    return text, in_tok, out_tok, elapsed


# ══════════════════════════════════════════════════════════════════════════════
# ЧТЕНИЕ ДАННЫХ ИЗ БД
# ══════════════════════════════════════════════════════════════════════════════

async def _load_recent_news(n: int) -> list[dict]:
    """
    Последние N опубликованных постов — только реальный контент из канала.
    Никакого fallback на raw_articles: только то что реально было опубликовано.
    """
    from db.database import async_session_factory
    from sqlalchemy import text

    async with async_session_factory() as session:
        rows = (await session.execute(text("""
            SELECT
                pp.post_text   AS old_text,
                pp.source_name AS source_name,
                pp.source_url  AS source_url,
                pp.published_at,
                ra.title       AS title,
                ra.content     AS content,
                ra.url         AS url
            FROM published_posts pp
            JOIN raw_articles ra ON ra.id = pp.article_id
            WHERE pp.source_name != 'arXiv'
              AND length(ra.content) > 300
            ORDER BY pp.id DESC
            LIMIT :n
        """), {"n": n})).fetchall()

    return [dict(r._mapping) for r in rows]


async def _load_recent_arxiv(n: int = 1) -> list[dict]:
    """Последние N arXiv-постов."""
    from db.database import async_session_factory
    from sqlalchemy import text

    async with async_session_factory() as session:
        rows = (await session.execute(text("""
            SELECT
                pp.post_text AS old_text,
                pp.published_at,
                ra.title     AS title,
                ra.content   AS content,
                ra.url       AS url
            FROM published_posts pp
            JOIN raw_articles ra ON ra.id = pp.article_id
            WHERE pp.source_name = 'arXiv'
            ORDER BY pp.id DESC
            LIMIT :n
        """), {"n": n})).fetchall()

    return [dict(r._mapping) for r in rows]


# ══════════════════════════════════════════════════════════════════════════════
# ФОРМАТИРОВАНИЕ ВЫВОДА
# ══════════════════════════════════════════════════════════════════════════════

WRAP = 68  # ширина переноса текста


def _print_text(text: str, indent: str = "  ") -> None:
    """Печатает текст с переносом длинных строк по словам."""
    # Убираем HTML-теги для чистого чтения
    clean = re.sub(r"<[^>]+>", "", text)
    for para in clean.split("\n"):
        para = para.strip()
        if not para:
            print()
            continue
        for line in textwrap.wrap(para, width=WRAP - len(indent)) or [""]:
            print(f"{indent}{line}")


def _box(title: str, text: str, width: int = 70) -> str:
    border = "─" * width
    lines  = []
    lines.append(f"┌{border}┐")
    lines.append(f"│ {title:<{width-1}}│")
    lines.append(f"├{border}┤")
    for line in text.split("\n"):
        # Разбиваем длинные строки
        while len(line) > width - 2:
            lines.append(f"│ {line[:width-2]} │")
            line = line[width-2:]
        lines.append(f"│ {line:<{width-2}} │")
    lines.append(f"└{border}┘")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

async def main() -> None:
    # Статьи 1..N//2 → BRIEF, остальные → ANALYSIS
    half = max(1, N_POSTS // 2)

    print("=" * 72)
    print(f"  test_voice.py v3 — {N_POSTS} статей")
    print(f"  [{half} × ✔️ БРИФ]  [{N_POSTS - half} × 📌 АНАЛИТИКА]")
    print("  Ничего не публикуется. Только сравнение.")
    print("=" * 72)

    api_key = os.environ.get("PERPLEXITY_API_KEY", "")
    if not api_key or not api_key.startswith("pplx-"):
        print("\n[!] PERPLEXITY_API_KEY не задан — выход")
        sys.exit(1)

    news = await _load_recent_news(N_POSTS)
    if not news:
        print("[!] Нет постов в БД")
        sys.exit(1)

    total_in = total_out = 0

    for i, row in enumerate(news):
        # Первая половина — BRIEF, вторая — ANALYSIS
        fmt = "brief" if i < half else "analysis"
        system_prompt = SYSTEM_BRIEF if fmt == "brief" else SYSTEM_ANALYSIS
        user_fn       = USER_BRIEF   if fmt == "brief" else USER_ANALYSIS
        icon          = "✔️" if fmt == "brief" else "📌"
        max_tok       = 300 if fmt == "brief" else 950

        url = row["url"] or row["source_url"] or ""

        print(f"\n{'═' * 72}")
        print(f"  [{i+1}/{len(news)}] {icon} {fmt.upper()}")
        print(f"  {row['title'][:65]}")
        print(f"  {row['source_name']} | {row['published_at']}")
        print(f"{'═' * 72}")

        # БЫЛО
        print("\n  ── БЫЛО " + "─" * 54)
        if row["old_text"]:
            _print_text(row["old_text"])
            print(f"\n  [{len(row['old_text'])} симв.]")
        else:
            print("  (нет опубликованного поста — статья из raw_articles)")

        # СТАЛО
        label = "✔️ БРИФ" if fmt == "brief" else "📌 АНАЛИТИКА"
        print(f"\n  ── СТАЛО ({label}) " + "─" * (46 - len(label)))
        try:
            ctx = "medium" if fmt == "analysis" else "low"
            new_text, in_tok, out_tok, elapsed = await _call_perplexity(
                system_prompt,
                user_fn(
                    row["title"],
                    row["content"] or "",
                    row["source_name"],
                    url,
                ),
                max_tokens=max_tok,
                search_context=ctx,
            )
            _print_text(new_text)
            print(f"\n  [{len(new_text)} симв. | {in_tok}in+{out_tok}out | {elapsed:.1f}с]")
            total_in  += in_tok
            total_out += out_tok
        except Exception as exc:
            print(f"  [ОШИБКА] {exc}")

    print(f"\n{'=' * 72}")
    print(f"  Итого {len(news)} постов | {total_in}in+{total_out}out токенов (~${(total_in+total_out)*0.000001:.4f})")
    print("  Если нравится — скажи, перенесём в writer.py.")
    print("=" * 72)


if __name__ == "__main__":
    asyncio.run(main())
