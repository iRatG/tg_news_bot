"""
Агент для публикации научных бумаг с arXiv — agents/arxiv_agent.py.

Использует официальный arxiv API (библиотека arxiv) вместо RSS —
надёжнее на VPS (нет SSL timeout, нет malformed XML).

Дедупликация через таблицу arxiv_seen_papers в БД вместо seen_papers.json.

Пайплайн для одной бумаги:
    fetch_new_papers()  → список новых (ещё не опубликованных) бумаг
    _extract_github()   → GitHub-ссылка из abstract (если есть)
    _summarize_paper()  → краткий обзор на русском через Perplexity sonar
    _format_html_post() → готовый HTML-пост для Telegram
    process_paper()     → объединяет всё вышеперечисленное
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
import time
from datetime import datetime, timedelta
from typing import Optional

import openai
from sqlalchemy import text

from core.config import settings
from db.database import async_session_factory

logger = logging.getLogger(__name__)


# ── Константы ─────────────────────────────────────────────────────────────────

ARXIV_QUERIES: list[str] = [
    "AI agents LLM",
    "multi-agent systems artificial intelligence",
    "autonomous agents reinforcement learning",
    "large language models reasoning",
    "agentic AI foundations",
]

ARXIV_CATEGORIES: list[str] = ["cs.AI", "cs.LG", "cs.MA", "stat.ML"]

ARXIV_MAX_PER_QUERY: int = 5
ARXIV_DAYS_LOOKBACK: int = 7  # ежедневный запуск — 7 дней достаточно

_GITHUB_RE = re.compile(
    r"https?://github\.com/[a-zA-Z0-9_.\-]+/[a-zA-Z0-9_.\-]+",
    re.IGNORECASE,
)

_PERPLEXITY_BASE_URL = "https://api.perplexity.ai"
_MODEL = "sonar"
_MAX_SUMMARY_CHARS = 800

_SYSTEM_PROMPT = (
    "Ты — научный редактор Telegram-канала об искусственном интеллекте. "
    "Специализируешься на агентных системах, LLM и математических методах в ИИ. "
    "Пишешь на русском языке — живо, понятно, увлекательно. "
    "Избегай сухого академического стиля. Читатель должен захотеть открыть статью."
)

_SUMMARY_PROMPT = """\
Напиши краткий обзор научной статьи для Telegram-канала об ИИ.

Название: {title}
Авторы: {authors}
Дата: {published}
Аннотация:
{abstract}

Требования:
- Строго до {max_chars} символов
- Язык: только русский
- Структура:
  📌 Яркая вводная фраза — о чём статья
  🔍 Что исследовали и зачем
  ⚙️ Как работает (метод, подход)
  📊 Ключевые результаты и цифры
  💡 Почему это важно / где применимо
- Не копируй аннотацию дословно
- Никаких сносок и цитат вида [1][2][3]"""


# ── Агент ─────────────────────────────────────────────────────────────────────

class ArxivAgent:
    """Агент для получения, суммаризации и форматирования бумаг arXiv."""

    # ── Вспомогательные методы ────────────────────────────────────────────────

    def _extract_github(self, abstract: str) -> Optional[str]:
        """Извлекает первую GitHub-ссылку из текста аннотации."""
        matches = _GITHUB_RE.findall(abstract)
        if not matches:
            return None
        return matches[0].rstrip(".,);")

    def _clean_arxiv_id(self, short_id: str) -> str:
        """Возвращает ID бумаги без версии: '2502.12345v1' → '2502.12345'."""
        return re.sub(r"v\d+$", "", short_id)

    def _strip_artifacts(self, text: str) -> str:
        """
        Убирает артефакты Perplexity из текста:
        - цитаты вида [1][2][3]
        - <br> → \\n
        - все HTML-теги
        - тройные+ переносы строк
        """
        text = re.sub(r"\[\d+\]", "", text)
        text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"<[^>]+>", "", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    # ── Получение новых бумаг ─────────────────────────────────────────────────

    async def fetch_new_papers(self) -> list[dict]:
        """
        Получает бумаги с arXiv API и фильтрует уже опубликованные.

        arxiv.Client() — синхронный, запускается через run_in_executor
        чтобы не блокировать event loop.

        Returns:
            Список словарей с метаданными новых бумаг.
        """
        loop = asyncio.get_event_loop()

        def _sync_fetch() -> list[dict]:
            """Синхронный блок: запрос к arXiv API."""
            import socket
            import arxiv  # импорт здесь — не в top-level, чтобы не ломать запуск без пакета

            # Обязательный timeout для синхронных HTTP запросов (arxiv использует requests)
            # Без этого requests может зависнуть навсегда на медленном соединении с RU VPS
            socket.setdefaulttimeout(60)

            cutoff = datetime.now() - timedelta(days=ARXIV_DAYS_LOOKBACK)
            seen_ids: set[str] = set()
            papers: list[dict] = []
            client = arxiv.Client()

            for query in ARXIV_QUERIES:
                cat_filter = " OR ".join(f"cat:{c}" for c in ARXIV_CATEGORIES)
                full_query = f"({query}) AND ({cat_filter})"

                search = arxiv.Search(
                    query=full_query,
                    max_results=ARXIV_MAX_PER_QUERY * 10,
                    sort_by=arxiv.SortCriterion.SubmittedDate,
                    sort_order=arxiv.SortOrder.Descending,
                )

                try:
                    count = 0
                    for paper in client.results(search):
                        if count >= ARXIV_MAX_PER_QUERY:
                            break
                        pub = paper.published.replace(tzinfo=None)
                        if pub < cutoff:
                            break  # отсортированы по дате desc — дальше только старее

                        short_id = paper.get_short_id()
                        if short_id in seen_ids:
                            continue
                        seen_ids.add(short_id)

                        arxiv_id = re.sub(r"v\d+$", "", short_id)
                        papers.append({
                            "arxiv_id":   arxiv_id,
                            "title":      paper.title,
                            "authors":    [a.name for a in paper.authors],
                            "abstract":   paper.summary,
                            "published":  paper.published.strftime("%Y-%m-%d"),
                            "categories": paper.categories,
                            "arxiv_url":  f"https://arxiv.org/abs/{short_id}",
                        })
                        count += 1

                except Exception as exc:
                    logger.warning(f"[arxiv_agent] Ошибка запроса '{query}': {exc}")

            return papers

        try:
            all_papers = await asyncio.wait_for(
                loop.run_in_executor(None, _sync_fetch),
                timeout=180.0,  # 3 минуты максимум
            )
        except asyncio.TimeoutError:
            logger.error("[arxiv_agent] Timeout при получении бумаг с arXiv (>180s) — пропускаем прогон")
            return []
        logger.info(f"[arxiv_agent] Получено с API: {len(all_papers)} бумаг")

        if not all_papers:
            return []

        # Фильтруем уже виденные по таблице arxiv_seen_papers
        all_ids = [p["arxiv_id"] for p in all_papers]
        placeholders = ", ".join(f":id{i}" for i in range(len(all_ids)))
        params = {f"id{i}": aid for i, aid in enumerate(all_ids)}

        async with async_session_factory() as session:
            rows = (await session.execute(
                text(f"SELECT arxiv_id FROM arxiv_seen_papers WHERE arxiv_id IN ({placeholders})"),
                params,
            )).fetchall()

        seen_in_db = {r[0] for r in rows}
        new_papers = [p for p in all_papers if p["arxiv_id"] not in seen_in_db]

        logger.info(
            f"[arxiv_agent] Новых бумаг: {len(new_papers)} "
            f"(отфильтровано виденных: {len(seen_in_db)})"
        )
        return new_papers

    # ── Суммаризация ──────────────────────────────────────────────────────────

    async def _summarize_paper(self, paper: dict) -> tuple[str, int, int]:
        """
        Генерирует краткий обзор бумаги на русском через Perplexity sonar.

        Returns:
            (summary_text, input_tokens, output_tokens)
        """
        authors = paper["authors"]
        authors_str = ", ".join(authors[:5])
        if len(authors) > 5:
            authors_str += f" и др."

        prompt = _SUMMARY_PROMPT.format(
            title=paper["title"],
            authors=authors_str,
            published=paper["published"],
            abstract=paper["abstract"][:2000],
            max_chars=_MAX_SUMMARY_CHARS,
        )

        client = openai.AsyncOpenAI(
            api_key=settings.PERPLEXITY_API_KEY,
            base_url=_PERPLEXITY_BASE_URL,
        )

        response = await client.chat.completions.create(
            model=_MODEL,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user",   "content": prompt},
            ],
            max_tokens=1500,
            temperature=0.7,
            extra_body={"web_search_options": {"search_context_size": "low"}},
        )

        summary = response.choices[0].message.content.strip()
        summary = self._strip_artifacts(summary)

        if len(summary) > _MAX_SUMMARY_CHARS:
            summary = summary[:_MAX_SUMMARY_CHARS - 3] + "..."

        in_tok  = response.usage.prompt_tokens     if response.usage else 0
        out_tok = response.usage.completion_tokens if response.usage else 0

        return summary, in_tok, out_tok

    # ── Форматирование HTML-поста ─────────────────────────────────────────────

    def _format_html_post(
        self,
        paper: dict,
        summary: str,
        github: Optional[str],
    ) -> str:
        """
        Формирует HTML-пост для Telegram.

        Формат:
            📄 <b>Title</b>

            👥 Author1, Author2 и др. (N авт.)
            📅 YYYY-MM-DD | 📂 cs.AI, cs.LG

            [summary]

            🔗 <a href="...">arXiv</a>
            💻 <a href="...">GitHub</a>  (если найден)
        """
        title_esc = html.escape(paper["title"])

        authors = paper["authors"]
        if len(authors) <= 3:
            authors_str = html.escape(", ".join(authors))
        else:
            first_three = html.escape(", ".join(authors[:3]))
            authors_str = f"{first_three} и др. ({len(authors)} авт.)"

        cats_str = html.escape(", ".join(paper["categories"][:3]))

        lines = [
            f"📄 <b>{title_esc}</b>",
            "",
            f"👥 {authors_str}",
            f"📅 {paper['published']} | 📂 {cats_str}",
            "",
            summary,
            "",
            f"🔗 <a href=\"{html.escape(paper['arxiv_url'])}\">arXiv</a>",
        ]

        if github:
            lines.append(f"💻 <a href=\"{html.escape(github)}\">GitHub</a>")

        return "\n".join(lines)

    # ── Полный цикл обработки одной бумаги ───────────────────────────────────

    async def process_paper(self, paper: dict) -> tuple[str, int, int]:
        """
        Обрабатывает одну бумагу: суммаризует и форматирует в HTML-пост.

        Returns:
            (html_post, input_tokens, output_tokens)
        """
        t0 = time.monotonic()
        github = self._extract_github(paper["abstract"])
        summary, in_tok, out_tok = await self._summarize_paper(paper)
        post_html = self._format_html_post(paper, summary, github)

        elapsed = int((time.monotonic() - t0) * 1000)
        github_str = github or "нет"
        logger.info(
            f"[arxiv_agent] Обработана бумага {paper['arxiv_id']!r} "
            f"({elapsed}мс, github={github_str[:40]})"
        )
        return post_html, in_tok, out_tok
