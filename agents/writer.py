from __future__ import annotations

"""
Агент 3 — Writer.

Пишет пост для Telegram-канала от лица senior data engineer.
Системный промпт читается из таблицы settings (ключ 'writer_system_prompt'),
что позволяет редактировать голос канала через admin-панель без передеплоя.

Алгоритм:
    1. Загружает системный промпт из БД (с fallback на захардкоженный default).
    2. Формирует user-запрос из заголовка, содержимого и источников верификации.
    3. Вызывает gpt-4o-mini и получает черновик поста 400-600 символов.
    4. Проверяет длину и наличие URL — предупреждает, но не блокирует.
    5. Возвращает WriterResult для передачи в Formatter.

Стоимость: ~$0.002/день (gpt-4o-mini, ~800 токенов/пост).
"""

import logging
import time
from dataclasses import dataclass

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

WRITER_MODEL       = "gpt-4o-mini"
MAX_TOKENS         = 600
TEMPERATURE        = 0.7    # Достаточно для стиля, не слишком случайный
MIN_POST_CHARS     = 300
MAX_POST_CHARS     = 700    # Мягкий потолок, жёсткий (1024) — в Formatter

# Fallback-промпт если settings недоступен
_DEFAULT_SYSTEM_PROMPT = """Ты — senior data engineer и AI practitioner с 10 годами опыта.
Пишешь пост для Telegram-канала об AI, LLM и vibe coding.

Структура поста (строго):
1. Эмодзи + Заголовок — 1 предложение, суть без воды
2. Что случилось — 2-3 предложения фактически
3. Почему это важно для нас — 1-2 предложения:
   что изменится в работе data engineer / AI-разработчика,
   какой инструмент устареет, что стоит попробовать прямо сейчас
4. 🔗 Источник: [название](url)

Язык: русский. Тон: умный коллега, не журналист.
Длина: 400-600 символов. Без "в заключении". Без воды."""


# ── Выходная структура ────────────────────────────────────────────────────────

@dataclass
class WriterResult:
    """Результат написания поста агентом Writer."""

    article_id:    int
    post_text:     str
    char_count:    int
    input_tokens:  int
    output_tokens: int
    latency_ms:    int

    def __repr__(self) -> str:
        return (
            f"<WriterResult article_id={self.article_id} "
            f"chars={self.char_count} "
            f"tokens={self.input_tokens}+{self.output_tokens}>"
        )


# ── Retry-декоратор ───────────────────────────────────────────────────────────

def _retryable(func):
    """3 попытки при RateLimitError / APIStatusError, иначе — немедленный выброс."""
    return retry(
        retry=retry_if_exception_type(
            (openai.RateLimitError, openai.APIStatusError)
        ),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=4, max=30),
        reraise=True,
    )(func)


# ── Формирование промпта ──────────────────────────────────────────────────────

def _build_user_prompt(
    article: RawArticleCandidate,
    verification: VerificationResult,
) -> str:
    """
    Формирует user-промпт с контекстом статьи и ссылками-подтверждениями.

    Передаём только первые 1000 символов content — достаточно для контекста
    и экономит токены gpt-4o-mini.
    """
    verified_by = ", ".join(verification.sources[:2]) if verification.sources else "—"
    return (
        f"Напиши пост об этой статье:\n\n"
        f"Заголовок: {article.title}\n"
        f"Содержание: {article.content[:1000]}\n"
        f"Источник: {article.source_name}\n"
        f"URL: {article.url}\n"
        f"Подтверждено: {verified_by}"
    )


# ── Основная логика ───────────────────────────────────────────────────────────

@_retryable
async def _call_gpt(system_prompt: str, user_prompt: str) -> tuple[str, int, int]:
    """
    Вызывает gpt-4o-mini и возвращает (текст, input_tokens, output_tokens).

    Raises:
        openai.RateLimitError: при превышении лимита (tenacity повторит).
        openai.APIError: при других ошибках API.
    """
    client = openai.AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
    response = await client.chat.completions.create(
        model=WRITER_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_prompt},
        ],
        temperature=TEMPERATURE,
        max_tokens=MAX_TOKENS,
    )
    text = response.choices[0].message.content.strip()
    in_tok  = getattr(response.usage, "prompt_tokens",     0)
    out_tok = getattr(response.usage, "completion_tokens", 0)
    return text, in_tok, out_tok


async def write_post(
    article: RawArticleCandidate,
    verification: VerificationResult,
) -> WriterResult:
    """
    Создаёт Telegram-пост от лица senior data engineer.

    Загружает системный промпт из БД (обновляемый через admin-панель).
    Предупреждает о нарушениях длины, но не блокирует — Formatter обрежет.

    Args:
        article:      Кандидат от Researcher.
        verification: Результат верификации от Fact-Checker.

    Returns:
        WriterResult с готовым текстом поста.
    """
    t0 = time.monotonic()
    logger.info(f"[writer] Написание поста: {article.title[:70]!r}")

    # Читаем промпт из БД — можно менять через admin без передеплоя
    system_prompt = await get_setting("writer_system_prompt", _DEFAULT_SYSTEM_PROMPT)
    user_prompt   = _build_user_prompt(article, verification)

    try:
        post_text, in_tok, out_tok = await _call_gpt(system_prompt, user_prompt)
    except Exception as exc:
        logger.error(f"[writer] Ошибка GPT API: {exc}")
        raise

    latency = int((time.monotonic() - t0) * 1000)
    char_count = len(post_text)

    # Диагностика длины — предупреждение без блокировки
    if char_count < MIN_POST_CHARS:
        logger.warning(
            f"[writer] Пост слишком короткий: {char_count} симв. "
            f"(мин. {MIN_POST_CHARS})"
        )
    elif char_count > MAX_POST_CHARS:
        logger.warning(
            f"[writer] Пост длинноват: {char_count} симв. "
            f"(рек. макс. {MAX_POST_CHARS}) — Formatter обрежет"
        )

    # Проверяем что URL источника присутствует в тексте
    if "http" not in post_text:
        logger.warning("[writer] В посте не найдена ссылка на источник")

    logger.info(
        f"[writer] OK: {char_count} симв. | "
        f"tokens={in_tok}+{out_tok} | {latency}мс"
    )

    return WriterResult(
        article_id=article.db_id,
        post_text=post_text,
        char_count=char_count,
        input_tokens=in_tok,
        output_tokens=out_tok,
        latency_ms=latency,
    )
