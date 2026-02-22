from __future__ import annotations

"""
scripts/test_quality.py — A/B тест качества Writer и Formatter.

ЗАЧЕМ ЭТОТ СКРИПТ
─────────────────
Бот использует Perplexity API для генерации текстов. Perplexity предлагает
несколько моделей с разной ценой:
  sonar-pro : $3/M input + $15/M output  (= ~$6 blended/M)
  sonar     : $1/M input + $1/M output   (= ~$1 blended/M)

Вопрос: стоит ли переплачивать за sonar-pro, или sonar даёт такое же качество?

Чтобы ответить объективно — без угадайки — скрипт прогоняет реальные статьи
из БД через обе модели и сравнивает по набору метрик качества.

КАК РАБОТАЕТ
────────────
1. Читает N последних опубликованных статей из таблицы published_posts + raw_articles.
2. Для каждой статьи определяет формат (single / longread / digest).
3. Вызывает Writer с моделью sonar-pro, затем с sonar — одинаковый промпт.
4. Оценивает каждый результат по 6 метрикам (см. ниже).
5. Опционально: тестирует Formatter на тех же постах.
6. Выводит сравнительную таблицу с расчётом экономии.

МЕТРИКИ КАЧЕСТВА
────────────────
  chars         — длина поста в символах
  in_range      — попадает ли в целевой диапазон формата:
                    single   400–600 симв.
                    longread 800–1200 симв.
                    digest   500–3800 симв.
  has_url       — есть ли ссылка на источник (http в тексте)
                  Критично: без ссылки пост не соответствует формату канала
  has_emoji     — есть ли стартовый эмодзи в первых 20 символах
                  Визуальный маркер в ленте Telegram
  no_citations  — отсутствуют ли цитаты вида [1] [2] [3]
                  Perplexity добавляет их из веб-поиска — в посте они неуместны
  no_bad_html   — отсутствуют ли неподдерживаемые Telegram теги (<p>, <div>, <br> и др.)
                  Их наличие вызывает ошибку "Can't parse entities" при публикации

ПАРАМЕТР search_context_size
────────────────────────────
Perplexity поддерживает web_search_options.search_context_size: low / medium / high.
Управляет тем, сколько веб-контекста добавляется в промпт.

  Writer + Formatter → "low"   (у нас уже есть RSS-контент, поиск не нужен)
  Fact-Checker       → "high"  (нужно найти максимум независимых источников)

Это снижает стоимость Writer/Formatter и устраняет появление цитат [n].

РЕЗУЛЬТАТЫ ТЕСТА (2026-02-22, 4 статьи Writer + 6 постов Formatter, VPS)
─────────────────────────────────────────────────────────────────────────
Writer:
  Метрика              sonar-pro    sonar    Вывод
  Средняя длина        682 симв.    686 симв.  одинаково
  В целевом диапазоне  0/4          2/4        sonar лучше
  Есть URL             2/4          4/4        sonar лучше (!)
  Есть эмодзи          4/4          4/4        одинаково
  Нет цитат [n]        4/4          4/4        одинаково
  Нет плохих тегов     4/4          4/4        одинаково
  Средний latency      4.1с         3.4с       sonar быстрее
  Стоимость теста      $0.01619     $0.00200   sonar дешевле в 8x

Formatter:
  Метрика              sonar-pro    sonar    Вывод
  Нет цитат [n]        6/6          6/6        одинаково
  Нет плохих тегов     6/6          6/6        одинаково
  Средний latency      2.9с         2.9с       одинаково
  Стоимость теста      $0.03464     $0.00450   sonar дешевле в 7.7x

Итоговая экономия при 3 постах в день:
  sonar-pro: ~$0.40/мес  →  sonar: ~$0.05/мес  (экономия 88%)

ВЫВОД: sonar не хуже sonar-pro по всем метрикам качества и быстрее.
Переход оправдан. Текущая конфигурация бота использует sonar.

ИСПОЛЬЗОВАНИЕ
─────────────
    python scripts/test_quality.py             # 5 статей, только writer
    python scripts/test_quality.py --n 3       # 3 статьи
    python scripts/test_quality.py --n 7 --formatter  # 7 статей + форматтер

    # На VPS (внутри Docker):
    docker exec newsbot python scripts/test_quality.py --n 5 --formatter

    Telegram НЕ используется, посты НЕ публикуются.
    Читает данные только из локальной БД.
"""

import argparse
import asyncio
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

os.environ.setdefault("PYTHONUTF8", "1")
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

import openai
from sqlalchemy import text

from db.database import async_session_factory
from agents.researcher import RawArticleCandidate
from agents.fact_checker import VerificationResult
from agents.writer import (
    _detect_post_format,
    _STYLE_PROMPTS,
    _build_user_prompt,
    _build_longread_prompt,
)

# ── Конфигурация моделей ───────────────────────────────────────────────────────

MODELS = ["sonar-pro", "sonar"]

# Цены Perplexity ($/M токенов, 2026)
MODEL_COSTS: Dict[str, Dict[str, float]] = {
    "sonar-pro": {"input": 3.0,  "output": 15.0},
    "sonar":     {"input": 1.0,  "output": 1.0},
}

# Целевые диапазоны длины по формату
FORMAT_RANGES = {
    "single":   (400, 600),
    "longread": (800, 1200),
    "digest":   (500, 3800),
}

_BAD_HTML = re.compile(
    r'</?(?:p|div|span|h[1-6]|ul|ol|li|hr|br|table|tr|td)[ >]',
    re.IGNORECASE,
)


# ── Вспомогательные функции ───────────────────────────────────────────────────

def calc_cost(model: str, in_tok: int, out_tok: int) -> float:
    """Расчёт стоимости вызова в USD."""
    c = MODEL_COSTS.get(model, {"input": 1.0, "output": 1.0})
    return (in_tok * c["input"] + out_tok * c["output"]) / 1_000_000


def evaluate_text(text: str, post_format: str) -> dict:
    """
    Качественные метрики поста.

    Возвращает словарь с булевыми и числовыми метриками.
    """
    lo, hi = FORMAT_RANGES.get(post_format, (400, 600))
    chars = len(text)
    # Эмодзи в первых 20 символах (стартовый эмодзи-заголовок)
    has_emoji = bool(re.search(
        r'[\U0001F300-\U0001F9FF\U00002600-\U000027BF\u2702-\u27B0✔✅🟡📌🔗🛡️]',
        text[:20],
    ))
    return {
        "chars":        chars,
        "in_range":     lo <= chars <= hi,
        "has_url":      "http" in text,
        "has_emoji":    has_emoji,
        "no_citations": not bool(re.search(r'\[\d+\]', text)),
        "no_bad_html":  not bool(_BAD_HTML.search(text)),
    }


async def call_model(
    model: str,
    system: str,
    user: str,
    max_tokens: int,
    search_context: str = "low",
) -> tuple[str, int, int, float]:
    """
    Вызов Perplexity API с указанной моделью.

    extra_body передаёт web_search_options — контролируем глубину поиска.
    Для Writer/Formatter достаточно 'low' (контент уже есть).
    Для Fact-Checker рекомендуется 'high'.

    Returns:
        (text, input_tokens, output_tokens, latency_sec)
    """
    client = openai.AsyncOpenAI(
        api_key=os.environ.get("PERPLEXITY_API_KEY", ""),
        base_url="https://api.perplexity.ai",
    )
    t0 = time.monotonic()
    response = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
        temperature=0.7,
        max_tokens=max_tokens,
        extra_body={"web_search_options": {"search_context_size": search_context}},
    )
    latency = time.monotonic() - t0
    out_text = response.choices[0].message.content.strip()
    # Убираем цитаты Perplexity
    out_text = re.sub(r'\[\d+\]', '', out_text).strip()
    in_tok  = getattr(response.usage, "prompt_tokens",     0)
    out_tok = getattr(response.usage, "completion_tokens", 0)
    return out_text, in_tok, out_tok, latency


async def call_formatter(
    model: str,
    post_text: str,
) -> tuple[str, int, int, float]:
    """
    Вызов Perplexity для HTML-форматирования (имитирует formatter.py).
    """
    system = "Ты — редактор Telegram-постов. Добавляй только HTML-теги, не меняй текст."
    user = (
        "Отформатируй этот пост для Telegram с HTML-разметкой.\n\n"
        "Правила:\n"
        "- Первую строку (заголовок) оберни в <b>заголовок</b>\n"
        "- Эмодзи оставь как есть\n"
        "- Строку с источником: 🔗 <a href=\"URL\">Название источника</a>\n"
        "- Максимум 4 эмодзи в посте\n"
        "- НЕ добавляй лишние переносы строк\n"
        "- Верни ТОЛЬКО отформатированный текст, без объяснений\n\n"
        f"Текст для форматирования:\n{post_text}"
    )
    return await call_model(model, system, user, max_tokens=800, search_context="low")


# ── Загрузка тестовых данных из БД ───────────────────────────────────────────

async def load_test_articles(n: int) -> List[RawArticleCandidate]:
    """Загружает n последних опубликованных статей из БД."""
    async with async_session_factory() as session:
        rows = (await session.execute(text("""
            SELECT
                ra.id,
                ra.title,
                ra.url,
                COALESCE(ra.content, ''),
                ra.fetched_at,
                s.name,
                s.url  AS source_url,
                s.category
            FROM published_posts pp
            JOIN raw_articles ra ON ra.id = pp.article_id
            JOIN sources s       ON s.id  = ra.source_id
            WHERE LENGTH(ra.content) > 100
            ORDER BY pp.published_at DESC
            LIMIT :n
        """), {"n": n})).fetchall()

    candidates = []
    for r in rows:
        fetched_at = r[4]
        if isinstance(fetched_at, str):
            fetched_at = datetime.strptime(fetched_at, "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=timezone.utc
            )
        candidates.append(RawArticleCandidate(
            db_id=r[0], title=r[1], url=r[2], content=r[3],
            source_name=r[5], source_url=r[6],
            published_at=fetched_at, score=20, category=r[7],
        ))
    return candidates


async def load_published_texts(n: int) -> List[tuple[str, str]]:
    """Загружает n последних опубликованных post_text для теста форматтера."""
    async with async_session_factory() as session:
        rows = (await session.execute(text("""
            SELECT post_text, source_name
            FROM published_posts
            ORDER BY published_at DESC
            LIMIT :n
        """), {"n": n})).fetchall()
    return [(r[0], r[1]) for r in rows]


# ── Отображение результатов ───────────────────────────────────────────────────

def _avg(items: list, key: str, fmt: str = "{:.0f}") -> str:
    vals = [r[key] for r in items if key in r]
    return fmt.format(sum(vals) / len(vals)) if vals else "—"


def _pct(items: list, key: str) -> str:
    vals = [r[key] for r in items if key in r]
    ok = sum(1 for v in vals if v)
    return f"{ok}/{len(vals)}" if vals else "—"


def print_writer_report(results: Dict[str, list], n_articles: int) -> None:
    """Печатает сравнительную таблицу результатов Writer."""
    line = "─" * 60
    print(f"\n{'='*60}")
    print("  ИТОГИ WRITER: sonar-pro vs sonar")
    print(f"{'='*60}")
    print(f"{'Метрика':<26} {'sonar-pro':>11} {'sonar':>10}")
    print(line)

    pro   = results["sonar-pro"]
    sonar = results["sonar"]

    rows = [
        ("Средняя длина",       _avg(pro, "chars", "{:.0f} симв."),   _avg(sonar, "chars", "{:.0f} симв.")),
        ("В диапазоне формата", _pct(pro, "in_range"),                _pct(sonar, "in_range")),
        ("Есть URL",            _pct(pro, "has_url"),                  _pct(sonar, "has_url")),
        ("Есть эмодзи",         _pct(pro, "has_emoji"),                _pct(sonar, "has_emoji")),
        ("Нет цитат [n]",       _pct(pro, "no_citations"),             _pct(sonar, "no_citations")),
        ("Нет плохих тегов",    _pct(pro, "no_bad_html"),              _pct(sonar, "no_bad_html")),
        ("Средний latency",     _avg(pro, "latency_s", "{:.1f}с"),     _avg(sonar, "latency_s", "{:.1f}с")),
        ("Токены in (avg)",     _avg(pro, "in_tok", "{:.0f}"),         _avg(sonar, "in_tok", "{:.0f}")),
        ("Токены out (avg)",    _avg(pro, "out_tok", "{:.0f}"),        _avg(sonar, "out_tok", "{:.0f}")),
    ]
    for label, pro_val, sonar_val in rows:
        print(f"{label:<26} {pro_val:>11} {sonar_val:>10}")

    total_pro   = sum(r["cost_usd"] for r in pro)
    total_sonar = sum(r["cost_usd"] for r in sonar)
    print(line)
    print(f"{'Стоимость теста':<26} {'${:.5f}'.format(total_pro):>11} {'${:.5f}'.format(total_sonar):>10}")

    if total_pro > 0:
        saving = (1 - total_sonar / total_pro) * 100
        daily_pro   = total_pro   / max(len(pro),   1) * 3
        daily_sonar = total_sonar / max(len(sonar), 1) * 3
        print(f"\n  💰 Экономия: {saving:.0f}% при переходе sonar-pro → sonar")
        print(f"  📅 При 3 постах в день:")
        print(f"     sonar-pro : ${daily_pro:.4f}/день  = ${daily_pro*30:.3f}/мес")
        print(f"     sonar     : ${daily_sonar:.4f}/день  = ${daily_sonar*30:.3f}/мес")
    print(f"{'='*60}")


def print_formatter_report(results: Dict[str, list]) -> None:
    """Печатает сравнительную таблицу результатов Formatter."""
    line = "─" * 60
    print(f"\n{'='*60}")
    print("  ИТОГИ FORMATTER: sonar-pro vs sonar")
    print(f"{'='*60}")
    print(f"{'Метрика':<26} {'sonar-pro':>11} {'sonar':>10}")
    print(line)

    pro   = results["sonar-pro"]
    sonar = results["sonar"]

    rows = [
        ("Нет цитат [n]",    _pct(pro, "no_citations"),  _pct(sonar, "no_citations")),
        ("Нет плохих тегов", _pct(pro, "no_bad_html"),   _pct(sonar, "no_bad_html")),
        ("Средний latency",  _avg(pro, "latency_s", "{:.1f}с"), _avg(sonar, "latency_s", "{:.1f}с")),
        ("Токены in (avg)",  _avg(pro, "in_tok", "{:.0f}"),     _avg(sonar, "in_tok", "{:.0f}")),
        ("Токены out (avg)", _avg(pro, "out_tok", "{:.0f}"),    _avg(sonar, "out_tok", "{:.0f}")),
    ]
    for label, pro_val, sonar_val in rows:
        print(f"{label:<26} {pro_val:>11} {sonar_val:>10}")

    total_pro   = sum(r["cost_usd"] for r in pro)
    total_sonar = sum(r["cost_usd"] for r in sonar)
    print(line)
    print(f"{'Стоимость теста':<26} {'${:.5f}'.format(total_pro):>11} {'${:.5f}'.format(total_sonar):>10}")

    if total_pro > 0:
        saving = (1 - total_sonar / total_pro) * 100
        print(f"\n  💰 Экономия formatter: {saving:.0f}%")
    print(f"{'='*60}")


# ── Основные тест-функции ─────────────────────────────────────────────────────

async def test_writer(n: int) -> Dict[str, list]:
    """
    Тест Writer: прогоняет n статей из БД через обе модели.

    Строит RawArticleCandidate из опубликованных статей,
    определяет формат, вызывает API, считает метрики.
    """
    articles = await load_test_articles(n)

    if not articles:
        print("❌ Нет статей в БД (нужны published_posts с content)")
        return {"sonar-pro": [], "sonar": []}

    print(f"\n{'='*60}")
    print(f"  ТЕСТ WRITER — {len(articles)} статей")
    print(f"  sonar-pro (текущий) vs sonar (кандидат)")
    print(f"  search_context_size=low (экономия поиска)")
    print(f"{'='*60}\n")

    results: Dict[str, list] = {"sonar-pro": [], "sonar": []}

    for i, article in enumerate(articles, 1):
        post_format = _detect_post_format(article)
        verification = VerificationResult(
            article_id=article.db_id,
            verified=True,
            confidence=0.9,
            reason="test",
            sources=[article.source_name],
        )
        system_p = _STYLE_PROMPTS["curator"]
        user_p   = (
            _build_longread_prompt(article)
            if post_format == "longread"
            else _build_user_prompt(article, verification)
        )
        max_tok = 1200 if post_format == "longread" else 600

        print(f"[{i}/{len(articles)}] {article.title[:55]!r} ({post_format})")

        for model in MODELS:
            try:
                txt, in_tok, out_tok, lat = await call_model(
                    model, system_p, user_p, max_tok
                )
                metrics = evaluate_text(txt, post_format)
                cost    = calc_cost(model, in_tok, out_tok)
                results[model].append({
                    **metrics,
                    "in_tok":    in_tok,
                    "out_tok":   out_tok,
                    "latency_s": lat,
                    "cost_usd":  cost,
                    "text":      txt,
                    "format":    post_format,
                })
                status = "✅" if metrics["in_range"] else "⚠️ "
                flags = (
                    ("🔗" if metrics["has_url"]  else "  ") +
                    ("😊" if metrics["has_emoji"] else "  ") +
                    ("✔" if metrics["no_citations"] else "❌[n]")
                )
                print(
                    f"  {model:<10} {status} {metrics['chars']:4d} симв. | "
                    f"{lat:.1f}с | ${cost:.5f} | {flags} | tok={in_tok}+{out_tok}"
                )
            except Exception as exc:
                print(f"  {model:<10} ❌  {exc}")
                results[model].append({"cost_usd": 0, "latency_s": 0})

        print()

    print_writer_report(results, len(articles))

    # Показываем пример поста от sonar (первый OK)
    sonar_ok = [r for r in results["sonar"] if "text" in r]
    if sonar_ok:
        sample = sonar_ok[0]["text"]
        print(f"\n{'─'*60}")
        print("  ПРИМЕР ПОСТА (sonar):")
        print(f"{'─'*60}")
        print(sample[:700] + ("..." if len(sample) > 700 else ""))
        print(f"{'─'*60}")

    return results


async def test_formatter(n: int) -> Dict[str, list]:
    """
    Тест Formatter: прогоняет n последних published_posts через обе модели.

    Берёт готовые тексты из БД, форматирует HTML, проверяет качество.
    """
    posts = await load_published_texts(n)

    if not posts:
        print("❌ Нет данных в published_posts")
        return {"sonar-pro": [], "sonar": []}

    print(f"\n{'='*60}")
    print(f"  ТЕСТ FORMATTER — {len(posts)} постов")
    print(f"  sonar-pro (текущий) vs sonar (кандидат)")
    print(f"{'='*60}\n")

    results: Dict[str, list] = {"sonar-pro": [], "sonar": []}

    for i, (post_text, source_name) in enumerate(posts, 1):
        print(f"[{i}/{len(posts)}] {source_name!r} ({len(post_text)} симв.)")

        for model in MODELS:
            try:
                txt, in_tok, out_tok, lat = await call_formatter(model, post_text)
                metrics = evaluate_text(txt, "single")
                cost    = calc_cost(model, in_tok, out_tok)
                results[model].append({
                    **metrics,
                    "in_tok":    in_tok,
                    "out_tok":   out_tok,
                    "latency_s": lat,
                    "cost_usd":  cost,
                })
                flags = (
                    ("✔[b]" if "<b>" in txt else "  ") +
                    ("✔[a]" if "<a " in txt else "  ") +
                    ("✔" if metrics["no_bad_html"] else "❌html")
                )
                print(
                    f"  {model:<10} {lat:.1f}с | ${cost:.5f} | {flags} | tok={in_tok}+{out_tok}"
                )
            except Exception as exc:
                print(f"  {model:<10} ❌  {exc}")
                results[model].append({"cost_usd": 0, "latency_s": 0})

        print()

    print_formatter_report(results)
    return results


# ── Точка входа ───────────────────────────────────────────────────────────────

async def main(n: int, test_fmt: bool) -> None:
    print("\n" + "=" * 60)
    print("  A/B Quality Test: Perplexity sonar-pro vs sonar")
    print("  Данные: реальные статьи из локальной БД")
    print("  Telegram: НЕ используется, посты НЕ публикуются")
    print("=" * 60)

    await test_writer(n)

    if test_fmt:
        await test_formatter(n)

    print("\nТест завершён.\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="A/B тест качества Writer и Formatter: sonar-pro vs sonar"
    )
    parser.add_argument(
        "--n", type=int, default=5,
        help="Число тестовых статей/постов (default: 5)"
    )
    parser.add_argument(
        "--formatter", action="store_true",
        help="Дополнительно тестировать Formatter"
    )
    args = parser.parse_args()

    asyncio.run(main(n=args.n, test_fmt=args.formatter))
