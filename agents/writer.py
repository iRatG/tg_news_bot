from __future__ import annotations

"""
Агент 3 — Writer.

Пишет пост для Telegram-канала в одном из 2 форматов: brief или analysis.
Автоматически выбирает формат по длине контента и ключевым словам.

Форматы постов:
    brief    — ✔️ короткий новостной бриф, 150-280 символов текста + ссылка
    analysis — 📌 аналитический разбор с секциями 🟡, 1200-1800 символов
    digest   — дайджест N новостей в формате ✔️ × N, до 3800 символов

Автовыбор формата:
    content > 800 симв  →  analysis
    ключевые слова       →  analysis (paper, research, arxiv, interview, ...)
    иначе                →  brief

Модель (с 2026-11-15): DeepSeek (core/llm.py). В веб сам не ходит — пишет
только по контенту статьи + verification.key_facts, которые Fact-Checker
(Perplexity с веб-поиском) уже нашёл в независимых источниках. Поэтому
промпты прямо запрещают факты сверх этих двух блоков.
temperature=0.3 — снижает галлюцинации по сравнению с 0.7.
"""

import logging
import time
from dataclasses import dataclass
from typing import List, Optional, Tuple

from agents.fact_checker import VerificationResult
from agents.researcher import RawArticleCandidate
from core.llm import deepseek_chat

logger = logging.getLogger(__name__)

# ── Конфигурация ──────────────────────────────────────────────────────────────

TEMPERATURE        = 0.3       # 0.3 вместо 0.7 — меньше галлюцинаций
MIN_POST_CHARS     = 200
MAX_BRIEF_CHARS    = 500       # мягкий потолок для brief
MAX_ANALYSIS_CHARS = 1800      # мягкий потолок для analysis
DIGEST_MAX_CHARS   = 3800      # лимит для дайджеста (с запасом до 4096)

# ── Общий DNA канала ───────────────────────────────────────────────────────────

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
    "Факты, числа, даты и названия бери ТОЛЬКО из присланного содержания источника "
    "и блока «Факты из веб-поиска». Чего там нет — того не пиши, даже если "
    "кажется, что знаешь: твои знания могут быть устаревшими."
)


def _facts_block(verification: Optional[VerificationResult], limit: int = 6) -> str:
    """Блок проверенных фактов из веб-поиска Fact-Checker'а (или пустая строка)."""
    facts = (verification.key_facts if verification else None) or []
    if not facts:
        return ""
    lines = "\n".join(f"- {f}" for f in facts[:limit])
    return f"Факты из веб-поиска (проверены по независимым источникам):\n{lines}\n"

# ── ФОРМАТ 1: БРИФ ────────────────────────────────────────────────────────────

SYSTEM_BRIEF = (
    _CHANNEL_DNA + "\n"
    "Пишешь короткие новостные брифы — только факты. "
    "Стиль: нейтральный, новостное агентство. "
    "Никакого анализа, никаких выводов, никаких призывов к действию."
)


def _build_brief_prompt(
    article: RawArticleCandidate,
    verification: VerificationResult,
) -> str:
    """User-промпт для краткого бриф-поста (brief)."""
    url = article.url or ""
    return (
        "Напиши новостной бриф для Telegram.\n\n"
        "Структура:\n"
        "✔️ <b>Заголовок</b>\n"
        "1-2 предложения: ключевые факты\n"
        f"🔗 <a href=\"{url}\">{article.source_name}</a>\n\n"
        "ПРАВИЛА:\n"
        f"— Заголовок ПРИДУМАЙ САМА на русском — НЕ копируй «{article.title[:60]}»\n"
        "  Стиль: «[Кто] [что сделал]» — например «OpenAI выпустила X»\n"
        "  НЕ призывы: «Используйте X», «Планируйте Y»\n"
        "— ОБЯЗАТЕЛЬНО начинай с ✔️ <b>Заголовок</b>\n"
        "— Длина текста: 150-250 символов (не считая ссылку)\n"
        "— Только факты из источника. Без выдуманных цифр. Без цитат [1][2][3]\n\n"
        f"Заголовок источника: {article.title}\n"
        f"Содержание: {article.content[:800]}\n"
        f"{_facts_block(verification, limit=3)}"
        f"Источник: {article.source_name}\n"
        f"URL: {url}"
    )


# ── ФОРМАТ 2: АНАЛИТИКА ───────────────────────────────────────────────────────

SYSTEM_ANALYSIS = (
    _CHANNEL_DNA + "\n"
    "Пишешь аналитические посты для разработчиков и AI-инженеров которые сами "
    "строят с LLM, RAG и inference каждый день. "
    "Задача: не пересказать новость, а раскрыть тему — что за этим стоит, "
    "как это работает, что меняется в реальной работе. "
    "Чтобы раскрыть тему глубже, используй блок «Факты из веб-поиска» — там "
    "конкретные числа, сравнения, контекст из независимых источников. "
    "Объяснять механизм и последствия можно своими словами, но любые конкретные "
    "цифры, даты, названия и сравнения — только из присланных материалов. "
    "Стиль: без маркетинга, без восторгов — как умный коллега который разобрался. "
    "Секция '🟡 Капля реализма' — только если есть реальное ограничение или противоречие."
)


def _build_analysis_prompt(
    article: RawArticleCandidate,
    verification: Optional[VerificationResult] = None,
) -> str:
    """User-промпт для аналитического поста (analysis). Цель: 1200-1800 симв."""
    url = article.url or ""
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
        f"🔗 <a href=\"{url}\">{article.source_name}</a>\n\n"
        "ПРАВИЛА:\n"
        f"— Заголовок ПРИДУМАЙ САМА на русском — НЕ копируй «{article.title[:60]}»\n"
        "— Весь текст — на русском, даже если оригинал на английском\n"
        "— Название 🟡 секции — отдельная строка, текст секции — следующая строка\n"
        "— Длина без ссылки: 1200-1800 символов — раскрой тему полностью\n"
        "— Только Telegram HTML: <b>, <i>, <a href=''>. Никаких **звёздочек**\n"
        "— Цифры и факты — только из содержания и фактов ниже. Не выдумывай\n"
        "— Если материала мало — пиши короче, но не добавляй фактов от себя\n"
        f"— Ссылку в конце пиши ТОЧНО ТАК: "
        f"🔗 <a href=\"{url}\">{article.source_name}</a> — не меняй URL\n\n"
        f"Заголовок: {article.title}\n"
        f"Содержание: {article.content[:1500]}\n"
        f"{_facts_block(verification)}"
        f"Источник: {article.source_name}\n"
        f"URL: {url}"
    )


# ── ДАЙДЖЕСТ ──────────────────────────────────────────────────────────────────

def _build_digest_prompt(
    articles: List[Tuple[RawArticleCandidate, VerificationResult]],
) -> str:
    """User-промпт для дайджеста из N статей."""
    n = len(articles)
    detail_hint = (
        "Описывай каждую новость подробнее (4-6 предложений)."
        if n <= 2 else
        "Описывай кратко (3-4 предложения)."
    )

    articles_block = ""
    for i, (article, verification) in enumerate(articles, 1):
        facts = (verification.key_facts if verification else None) or []
        facts_str = "".join(f"     - {f}\n" for f in facts[:3])
        articles_block += (
            f"---\n"
            f"{i}. Заголовок: {article.title}\n"
            f"   Источник: {article.source_name}\n"
            f"   URL: {article.url}\n"
            f"   Контент: {article.content[:600]}\n"
            + (f"   Факты из веб-поиска:\n{facts_str}" if facts_str else "")
            + "\n"
        )

    return (
        f"Создай дайджест {n} новостей для Telegram-канала об AI.\n\n"
        f"{detail_hint}\n\n"
        "Формат каждой новости:\n"
        "✔️ <b>Заголовок на русском</b> — 1 строка\n"
        "[2-4 предложения с сутью]\n"
        "🔗 <a href=\"[URL этой статьи]\">[домен или название источника этой статьи]</a>\n\n"
        "Между новостями — пустая строка.\n"
        f"Весь пост — до {DIGEST_MAX_CHARS} символов.\n"
        "Язык: русский. Без лишних слов. Без цитат [1][2][3].\n"
        "Факты — только из контента и фактов каждой новости, ничего от себя.\n"
        "Только Telegram HTML: <b>, <i>, <a href=''>. Никаких **звёздочек**.\n\n"
        f"Новости:\n{articles_block}"
    )


# ── Определение формата поста ─────────────────────────────────────────────────

_ANALYSIS_KEYWORDS = {
    "interview", "podcast", "research", "paper", "study", "survey",
    "report", "benchmark", "analysis", "arxiv", "deep dive", "deep-dive",
    "интервью", "подкаст", "исследование", "доклад", "анализ", "бенчмарк",
    "обзор", "разбор",
}


def _ensure_source_link(post_text: str, url: str, source_name: str) -> str:
    """
    Гарантирует наличие ссылки на источник в посте.

    LLM периодически игнорирует инструкцию вставить <a href>, и тогда
    Analyst отбрасывает уже готовый пост ('отсутствует ссылка на источник') —
    впустую сгорает весь пайплайн (fact-check + writer + formatter токены).
    URL всегда известен, поэтому дописываем ссылку детерминированно.
    """
    if "http" in post_text:
        return post_text
    url = (url or "").strip()
    if not url:
        return post_text  # нечего вставлять — пусть решает Analyst
    label = source_name or "Источник"
    return post_text.rstrip() + f'\n\n🔗 <a href="{url}">{label}</a>'


def _detect_post_format(article: RawArticleCandidate) -> str:
    """
    Определяет формат поста: 'brief' или 'analysis'.

    analysis если: ключевое слово в заголовке, arxiv в URL источника,
    или контент длиннее 800 символов.
    """
    title_lower = article.title.lower()
    if any(kw in title_lower for kw in _ANALYSIS_KEYWORDS):
        return "analysis"
    if "arxiv" in article.source_url.lower():
        return "analysis"
    if len(article.content) > 800:
        return "analysis"
    return "brief"


# ── Выходная структура ────────────────────────────────────────────────────────

@dataclass
class WriterResult:
    """Результат написания поста агентом Writer."""

    article_id:    int
    post_text:     str
    char_count:    int
    post_format:   str   # 'brief' | 'analysis' | 'digest'
    input_tokens:  int
    output_tokens: int
    latency_ms:    int

    def __repr__(self) -> str:
        return (
            f"<WriterResult article_id={self.article_id} "
            f"format={self.post_format} chars={self.char_count} "
            f"tokens={self.input_tokens}+{self.output_tokens}>"
        )


# ── Публичный интерфейс ───────────────────────────────────────────────────────

async def write_post(
    article: RawArticleCandidate,
    verification: VerificationResult,
) -> WriterResult:
    """
    Создаёт Telegram-пост в формате brief или analysis.

    Автовыбор формата по длине контента (> 800 симв → analysis)
    и ключевым словам (paper, research, arxiv → analysis).

    Args:
        article:      Кандидат от Researcher.
        verification: Результат верификации от Fact-Checker.

    Returns:
        WriterResult с готовым текстом и полем post_format.
    """
    t0 = time.monotonic()

    post_format = _detect_post_format(article)

    logger.info(
        f"[writer] Написание поста: format={post_format} "
        f"{article.title[:60]!r}"
    )

    # max_tokens с запасом: русский текст ~2-3 симв/токен, плюс HTML-теги и URL
    if post_format == "analysis":
        system_prompt   = SYSTEM_ANALYSIS
        user_prompt     = _build_analysis_prompt(article, verification)
        max_tokens      = 1800
    else:
        system_prompt   = SYSTEM_BRIEF
        user_prompt     = _build_brief_prompt(article, verification)
        max_tokens      = 500

    try:
        post_text, in_tok, out_tok = await deepseek_chat(
            system_prompt, user_prompt,
            max_tokens=max_tokens,
            temperature=TEMPERATURE,
        )
    except Exception as exc:
        logger.error(f"[writer] Ошибка DeepSeek API: {exc}")
        raise

    # Страховка: если модель не вставила ссылку — дописываем детерминированно,
    # чтобы Analyst не отбросил готовый пост из-за отсутствия <a href>.
    if "http" not in post_text:
        logger.warning("[writer] Ссылка не найдена — вставляю источник детерминированно")
        post_text = _ensure_source_link(post_text, article.url, article.source_name)

    latency    = int((time.monotonic() - t0) * 1000)
    char_count = len(post_text)

    if char_count < MIN_POST_CHARS:
        logger.warning(f"[writer] Пост слишком короткий: {char_count} симв.")

    if post_format == "brief" and char_count > MAX_BRIEF_CHARS:
        logger.warning(f"[writer] Бриф длинноват: {char_count} симв.")
    elif post_format == "analysis" and char_count > MAX_ANALYSIS_CHARS:
        logger.warning(f"[writer] Аналитика длинновата: {char_count} симв. — Formatter обрежет")

    logger.info(
        f"[writer] OK: format={post_format} "
        f"{char_count} симв. | tokens={in_tok}+{out_tok} | {latency}мс"
    )

    return WriterResult(
        article_id=article.db_id,
        post_text=post_text,
        char_count=char_count,
        post_format=post_format,
        input_tokens=in_tok,
        output_tokens=out_tok,
        latency_ms=latency,
    )


async def write_digest(
    articles: List[Tuple[RawArticleCandidate, VerificationResult]],
) -> WriterResult:
    """
    Пишет единый дайджест из N верифицированных статей.

    Использует curator-стиль (нейтральный, informative).
    article_id в результате — ID первой статьи (primary).

    Args:
        articles: Список пар (кандидат, верификация) из pipeline.

    Returns:
        WriterResult с post_format='digest'.
    """
    t0 = time.monotonic()
    n  = len(articles)
    primary_article = articles[0][0]

    logger.info(f"[writer] Написание дайджеста из {n} статей")

    system_prompt = SYSTEM_BRIEF  # дайджест = сборник брифов
    user_prompt   = _build_digest_prompt(articles)

    try:
        # До 3800 симв. русского текста + теги/URL ≈ 2000 токенов — берём с запасом
        post_text, in_tok, out_tok = await deepseek_chat(
            system_prompt, user_prompt,
            max_tokens=3000,
            temperature=TEMPERATURE,
        )
    except Exception as exc:
        logger.error(f"[writer] Ошибка DeepSeek API (дайджест): {exc}")
        raise

    # Страховка: дайджест без единой ссылки — дописываем ссылку primary-статьи.
    if "http" not in post_text:
        logger.warning("[writer] В дайджесте нет ссылок — вставляю primary-источник")
        post_text = _ensure_source_link(
            post_text, primary_article.url, primary_article.source_name
        )

    latency    = int((time.monotonic() - t0) * 1000)
    char_count = len(post_text)

    logger.info(
        f"[writer] Дайджест OK: {n} новостей | {char_count} симв. | "
        f"tokens={in_tok}+{out_tok} | {latency}мс"
    )

    return WriterResult(
        article_id=primary_article.db_id,
        post_text=post_text,
        char_count=char_count,
        post_format="digest",
        input_tokens=in_tok,
        output_tokens=out_tok,
        latency_ms=latency,
    )
