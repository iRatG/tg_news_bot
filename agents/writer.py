from __future__ import annotations

"""
Агент 3 — Writer.

Пишет пост для Telegram-канала в одном из 4 стилей с автоматической ротацией.
Поддерживает 3 формата: single (одиночный), longread (глубокий разбор), digest (дайджест).

Алгоритм (одиночный пост):
    1. Определяет формат: single или longread по ключевым словам и длине контента.
    2. Читает текущий стиль из settings, сохраняет следующий (ротация).
    3. Вызывает Perplexity sonar-pro с выбранным системным промптом.
    4. Возвращает WriterResult с полем post_format.

Алгоритм (дайджест):
    1. Принимает список (статья, верификация) от pipeline (утренний прогон).
    2. Всегда использует стиль 'curator' — без ротации.
    3. Строит один пост из N новостей в формате ✔️ × N.
    4. post_format = 'digest'.

Форматы постов:
    single   — одна новость, 400-600 символов
    longread — структурированный разбор через 🟡 разделы, 800-1200 символов
    digest   — дайджест N новостей в формате ✔️ × N, до 3800 символов

Стили (ротация curator → tech_analyst → practitioner → skeptic → ...):
    curator      — нейтральный, информативный, без оценок
    tech_analyst — технический разбор изнутри
    practitioner — что применимо прямо сейчас
    skeptic      — ограничения и что умолчали

Примечание: Perplexity sonar доступен глобально с RU VPS.
A/B тест (2026-02-22): sonar не хуже sonar-pro, на 88% дешевле, URL 4/4 vs 2/4.
DeepSeek НЕ работает внутри Docker-контейнера на VPS (Connection error, 2026-02-22).
Стоимость: ~$0.0005/день (sonar, ~500 токенов/пост).
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

WRITER_MODEL     = "sonar"        # sonar дешевле и быстрее sonar-pro; A/B тест: -88% цена, URL 4/4 vs 2/4
TEMPERATURE      = 0.7
MIN_POST_CHARS   = 300
MAX_POST_CHARS   = 700    # Мягкий потолок для single, жёсткий — в Formatter
DIGEST_MAX_CHARS = 3800   # Целевой лимит для дайджеста (с запасом до 4096)

# ── Стили ─────────────────────────────────────────────────────────────────────

STYLES: List[str] = ["curator", "tech_analyst", "practitioner", "skeptic"]

_STYLE_PROMPTS = {
    "curator": (
        "Ты — редактор Telegram-канала об AI и LLM. "
        "Задача: коротко и точно подать новость. Без оценок и воды. "
        "Тон: нейтральный, информативный."
    ),
    "tech_analyst": (
        "Ты — senior ML engineer с 10 годами опыта. "
        "Разбираешь новость изнутри: архитектура, числа, технические детали. "
        "Объясняешь как это работает и что изменилось. Используй разделы 🟡 если нужна структура. "
        "Тон: инженер объясняет инженеру."
    ),
    "practitioner": (
        "Ты — AI practitioner и senior data engineer. "
        "Фокус: что из этой новости применимо прямо сейчас. "
        "Что попробовать, что устарело, как это меняет рабочий процесс. "
        "Прямые рекомендации практика к практику."
    ),
    "skeptic": (
        "Ты — технический аналитик с критическим взглядом. "
        "Честно называешь ограничения, что преувеличено, что умолчали. "
        "Конструктивный скептик — не хейтер, а честный коллега. "
        'Фраза "Капля реализма:" уместна где нужно.'
    ),
}

# ── Определение формата поста ─────────────────────────────────────────────────

_LONGREAD_KEYWORDS = {
    "interview", "podcast", "research", "paper", "study", "survey",
    "report", "benchmark", "analysis", "arxiv", "deep dive", "deep-dive",
    "интервью", "подкаст", "исследование", "доклад", "анализ", "бенчмарк",
    "обзор", "разбор",
}


def _detect_post_format(article: RawArticleCandidate) -> str:
    """
    Определяет формат поста: 'single' или 'longread'.

    Longread если: ключевое слово в заголовке, arxiv в URL источника,
    или контент длиннее 2000 символов.
    """
    title_lower = article.title.lower()
    if any(kw in title_lower for kw in _LONGREAD_KEYWORDS):
        return "longread"
    if "arxiv" in article.source_url.lower():
        return "longread"
    if len(article.content) > 2000:
        return "longread"
    return "single"


# ── Ротация стилей ────────────────────────────────────────────────────────────

async def _rotate_style() -> str:
    """
    Возвращает текущий стиль и записывает следующий в settings.

    Ротация: curator → tech_analyst → practitioner → skeptic → curator → ...
    Вызывается только для single/longread. Дайджест использует 'curator' напрямую.
    """
    from core.config import set_setting
    current = await get_setting("post_style_current", "curator")
    if current not in STYLES:
        current = "curator"
    idx = STYLES.index(current)
    next_style = STYLES[(idx + 1) % len(STYLES)]
    await set_setting("post_style_current", next_style)
    return current


# ── Выходная структура ────────────────────────────────────────────────────────

@dataclass
class WriterResult:
    """Результат написания поста агентом Writer."""

    article_id:    int
    post_text:     str
    char_count:    int
    post_format:   str   # 'single' | 'longread' | 'digest'
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
) -> tuple[str, int, int]:
    """
    Вызывает Perplexity sonar-pro через OpenAI-совместимый API.

    Perplexity доступен глобально с RU VPS.
    DeepSeek НЕ работает внутри Docker-контейнера на VPS (Connection error, 2026-02-22).

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
            # Минимальный контекст поиска — Writer использует RSS-контент,
            # веб-поиск не нужен. Снижает стоимость и устраняет цитаты.
            "web_search_options": {"search_context_size": "low"},
        },
    )
    text    = response.choices[0].message.content.strip()
    # Убираем цитаты Perplexity вида [1], [2][3] — они не нужны в постах
    text    = re.sub(r'\[\d+\]', '', text).strip()
    in_tok  = getattr(response.usage, "prompt_tokens",     0)
    out_tok = getattr(response.usage, "completion_tokens", 0)
    return text, in_tok, out_tok


# ── Формирование промптов ─────────────────────────────────────────────────────

def _build_user_prompt(
    article: RawArticleCandidate,
    verification: VerificationResult,
) -> str:
    """User-промпт для одиночного поста (single)."""
    verified_by = ", ".join(verification.sources[:2]) if verification.sources else "—"
    return (
        "Напиши пост об этой статье.\n\n"
        "Структура:\n"
        "1. Эмодзи + Заголовок — 1 строка, суть\n"
        "2. Что произошло — 2-3 предложения\n"
        "3. Почему важно — 1-2 предложения\n"
        "4. 🔗 Источник: источник.com (URL)\n\n"
        "Язык: русский. Длина: 400-600 символов. Без \"в заключении\", без воды. "
        "Без цитат в формате [1][2][3].\n\n"
        f"Заголовок: {article.title}\n"
        f"Содержание: {article.content[:1000]}\n"
        f"Источник: {article.source_name}\n"
        f"URL: {article.url}\n"
        f"Подтверждено: {verified_by}"
    )


def _build_longread_prompt(article: RawArticleCandidate) -> str:
    """User-промпт для лонгрида (longread)."""
    return (
        "Создай структурированный разбор для Telegram.\n\n"
        "Структура:\n"
        "- Первая строка: 📌 Заголовок\n"
        "- 3-5 разделов через 🟡 Название раздела (3-5 предложений каждый)\n"
        "- Финал: источник.com (URL)\n\n"
        "Язык: русский. Длина: 800-1200 символов. Без \"в заключении\". "
        "Без цитат в формате [1][2][3].\n\n"
        f"Заголовок: {article.title}\n"
        f"Содержание: {article.content[:2000]}\n"
        f"Источник: {article.source_name}\n"
        f"URL: {article.url}"
    )


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
        "Формат для каждой новости:\n"
        "✔️ Заголовок — 1 строка\n"
        "[предложения с сутью]\n"
        "источник.com (URL)\n\n"
        "Между новостями — пустая строка.\n"
        f"Весь пост — до {DIGEST_MAX_CHARS} символов.\n"
        "Язык: русский. Без лишних слов. Без цитат в формате [1][2][3].\n\n"
        f"Новости:\n{articles_block}"
    )


# ── Публичный интерфейс ───────────────────────────────────────────────────────

async def write_post(
    article: RawArticleCandidate,
    verification: VerificationResult,
) -> WriterResult:
    """
    Создаёт Telegram-пост в ротируемом стиле (single или longread).

    Определяет формат по ключевым словам и длине контента.
    Ротирует стиль: curator → tech_analyst → practitioner → skeptic → ...
    writer_system_prompt из DB применяется только если он был вручную изменён.

    Args:
        article:      Кандидат от Researcher.
        verification: Результат верификации от Fact-Checker.

    Returns:
        WriterResult с готовым текстом и полем post_format.
    """
    t0 = time.monotonic()

    post_format = _detect_post_format(article)
    style       = await _rotate_style()

    logger.info(
        f"[writer] Написание поста: format={post_format} style={style} "
        f"{article.title[:60]!r}"
    )

    # Системный промпт: для single берём из DB если вручную изменён,
    # иначе используем стилевой. Для longread — всегда стилевой.
    db_prompt = await get_setting("writer_system_prompt", "")
    if post_format == "single" and db_prompt and "senior data engineer и AI practitioner" not in db_prompt:
        system_prompt = db_prompt
    else:
        system_prompt = _STYLE_PROMPTS[style]

    user_prompt = (
        _build_longread_prompt(article)
        if post_format == "longread"
        else _build_user_prompt(article, verification)
    )

    max_tokens = 1200 if post_format == "longread" else 600

    try:
        post_text, in_tok, out_tok = await _call_perplexity(
            system_prompt, user_prompt, max_tokens=max_tokens
        )
    except Exception as exc:
        logger.error(f"[writer] Ошибка Perplexity API: {exc}")
        raise

    latency    = int((time.monotonic() - t0) * 1000)
    char_count = len(post_text)

    if char_count < MIN_POST_CHARS:
        logger.warning(f"[writer] Пост слишком короткий: {char_count} симв.")
    elif post_format == "single" and char_count > MAX_POST_CHARS:
        logger.warning(f"[writer] Пост длинноват: {char_count} симв. — Formatter обрежет")

    if "http" not in post_text:
        logger.warning("[writer] В посте не найдена ссылка на источник")

    logger.info(
        f"[writer] OK: format={post_format} style={style} "
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

    Всегда использует стиль 'curator' — нейтральный и информативный.
    Не меняет счётчик ротации стилей (ротация только для write_post).
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

    system_prompt = _STYLE_PROMPTS["curator"]
    user_prompt   = _build_digest_prompt(articles)

    try:
        post_text, in_tok, out_tok = await _call_perplexity(
            system_prompt, user_prompt, max_tokens=1500
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
