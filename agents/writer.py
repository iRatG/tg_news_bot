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

Примечание: Perplexity sonar доступен глобально с RU VPS.
A/B тест (2026-02-22): sonar не хуже sonar-pro, на 88% дешевле, URL 4/4 vs 2/4.
temperature=0.3 — снижает галлюцинации по сравнению с 0.7.
DeepSeek НЕ работает внутри Docker-контейнера на VPS (Connection error, 2026-02-22).
"""

import logging
import re
import time
from dataclasses import dataclass
from typing import List, Optional, Tuple

import openai
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from agents.fact_checker import VerificationResult
from agents.researcher import RawArticleCandidate
from core.config import get_setting, settings

logger = logging.getLogger(__name__)

# ── Конфигурация ──────────────────────────────────────────────────────────────

WRITER_MODEL       = "sonar"   # sonar дешевле sonar-pro; A/B тест 2026-02-22: -88% цена, URL 4/4
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
    "Используй реальные числа и факты. Не выдумывай — только то что реально существует."
)

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
        f"источник.com (<a href=\"{url}\">{url}</a>)\n\n"
        "ПРАВИЛА:\n"
        f"— Заголовок ПРИДУМАЙ САМА на русском — НЕ копируй «{article.title[:60]}»\n"
        "  Стиль: «[Кто] [что сделал]» — например «OpenAI выпустила X»\n"
        "  НЕ призывы: «Используйте X», «Планируйте Y»\n"
        "— ОБЯЗАТЕЛЬНО начинай с ✔️ <b>Заголовок</b>\n"
        "— Длина текста: 150-250 символов (не считая ссылку)\n"
        "— Только факты из источника. Без выдуманных цифр. Без цитат [1][2][3]\n\n"
        f"Заголовок источника: {article.title}\n"
        f"Содержание: {article.content[:800]}\n"
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
    "Ты можешь использовать реальные факты из открытых источников чтобы раскрыть тему глубже — "
    "конкретные числа, сравнения, контекст. Главное: только реально проверяемые факты, "
    "не выдуманные. "
    "Стиль: без маркетинга, без восторгов — как умный коллега который разобрался. "
    "Секция '🟡 Капля реализма' — только если есть реальное ограничение или противоречие."
)


def _build_analysis_prompt(article: RawArticleCandidate) -> str:
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
        "— Используй реальные числа и факты. Не выдумывай\n"
        f"— Ссылку в конце пиши ТОЧНО ТАК: "
        f"🔗 <a href=\"{url}\">{article.source_name}</a> — не меняй URL\n\n"
        f"Заголовок: {article.title}\n"
        f"Содержание: {article.content[:1500]}\n"
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
    for i, (article, _) in enumerate(articles, 1):
        articles_block += (
            f"---\n"
            f"{i}. Заголовок: {article.title}\n"
            f"   Источник: {article.source_name}\n"
            f"   URL: {article.url}\n"
            f"   Контент: {article.content[:600]}\n\n"
        )

    return (
        f"Создай дайджест {n} новостей для Telegram-канала об AI.\n\n"
        f"{detail_hint}\n\n"
        "Формат каждой новости:\n"
        "✔️ <b>Заголовок на русском</b> — 1 строка\n"
        "[2-4 предложения с сутью]\n"
        "источник.com (URL)\n\n"
        "Между новостями — пустая строка.\n"
        f"Весь пост — до {DIGEST_MAX_CHARS} символов.\n"
        "Язык: русский. Без лишних слов. Без цитат [1][2][3].\n"
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


# ── Retry-декоратор ───────────────────────────────────────────────────────────

def _retryable(func):
    """3 попытки при RateLimitError / APIStatusError."""
    return retry(
        retry=retry_if_exception_type(
            (openai.RateLimitError, openai.APIStatusError)
        ),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=4, max=30),
        reraise=True,
    )(func)


# ── Вызов Perplexity API ──────────────────────────────────────────────────────

@_retryable
async def _call_perplexity(
    system_prompt: str,
    user_prompt: str,
    max_tokens: int = 600,
    search_context: str = "low",
) -> tuple[str, int, int]:
    """
    Вызывает Perplexity sonar через OpenAI-совместимый API.

    Perplexity доступен глобально с RU VPS.
    search_context:
        "low"    — для brief (только факты из RSS-контента)
        "medium" — для analysis (веб-поиск для обогащения деталями)

    Returns:
        (текст, input_tokens, output_tokens)
    """
    client = openai.AsyncOpenAI(
        api_key=settings.PERPLEXITY_API_KEY,
        base_url="https://api.perplexity.ai",
    )
    response = await client.chat.completions.create(
        model=WRITER_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_prompt},
        ],
        temperature=TEMPERATURE,
        max_tokens=max_tokens,
        extra_body={
            "web_search_options": {"search_context_size": search_context},
        },
    )
    text    = response.choices[0].message.content.strip()
    # Убираем цитаты Perplexity вида [1], [2][3]
    text    = re.sub(r'\[\d+\]', '', text).strip()
    in_tok  = getattr(response.usage, "prompt_tokens",     0)
    out_tok = getattr(response.usage, "completion_tokens", 0)
    return text, in_tok, out_tok


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

    if post_format == "analysis":
        system_prompt   = SYSTEM_ANALYSIS
        user_prompt     = _build_analysis_prompt(article)
        max_tokens      = 950
        search_context  = "medium"
    else:
        system_prompt   = SYSTEM_BRIEF
        user_prompt     = _build_brief_prompt(article, verification)
        max_tokens      = 300
        search_context  = "low"

    try:
        post_text, in_tok, out_tok = await _call_perplexity(
            system_prompt, user_prompt,
            max_tokens=max_tokens,
            search_context=search_context,
        )
    except Exception as exc:
        logger.error(f"[writer] Ошибка Perplexity API: {exc}")
        raise

    latency    = int((time.monotonic() - t0) * 1000)
    char_count = len(post_text)

    if char_count < MIN_POST_CHARS:
        logger.warning(f"[writer] Пост слишком короткий: {char_count} симв.")

    if post_format == "brief" and char_count > MAX_BRIEF_CHARS:
        logger.warning(f"[writer] Бриф длинноват: {char_count} симв.")
    elif post_format == "analysis" and char_count > MAX_ANALYSIS_CHARS:
        logger.warning(f"[writer] Аналитика длинновата: {char_count} симв. — Formatter обрежет")

    if "http" not in post_text:
        logger.warning("[writer] В посте не найдена ссылка на источник")

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
        post_text, in_tok, out_tok = await _call_perplexity(
            system_prompt, user_prompt,
            max_tokens=1500,
            search_context="low",
        )
    except Exception as exc:
        logger.error(f"[writer] Ошибка Perplexity API (дайджест): {exc}")
        raise

    latency    = int((time.monotonic() - t0) * 1000)
    char_count = len(post_text)

    if "http" not in post_text:
        logger.warning("[writer] В дайджесте не найдены ссылки на источники")

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
