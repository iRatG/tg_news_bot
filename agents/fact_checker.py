"""
Агент 2 — Fact-Checker.

Верифицирует статьи через Perplexity Sonar API, который выполняет
поиск в реальном времени и находит независимые подтверждения.

Алгоритм:
    1. Получает статью-кандидат от Researcher.
    2. Отправляет запрос в Perplexity sonar с просьбой найти 2-3 источника.
    3. Парсит JSON-ответ: verified, confidence, reasoning, sources.
    4. Применяет правила отклонения: confidence < 0.65, дубликат темы,
       или явное опровержение.
    5. Записывает результат в agent_logs.

Стоимость: ~$0.005/статья → ~$0.025/день (5 кандидатов).
"""

import json
import logging
import time
from dataclasses import dataclass, field
from typing import List, Optional

import openai
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from agents.researcher import RawArticleCandidate
from core.config import settings

logger = logging.getLogger(__name__)

# ── Конфигурация ──────────────────────────────────────────────────────────────

PERPLEXITY_MODEL   = "sonar"          # Модель с real-time веб-поиском
MIN_CONFIDENCE     = 0.65             # Минимальный порог достоверности
MAX_CONTENT_CHARS  = 500              # Сколько символов content отправлять


# ── Выходная структура ────────────────────────────────────────────────────────

@dataclass
class VerificationResult:
    """Результат верификации статьи через Perplexity Sonar."""

    article_id:    int
    verified:      bool
    confidence:    float
    reason:        str
    sources:       List[str] = field(default_factory=list)
    input_tokens:  int = 0
    output_tokens: int = 0
    latency_ms:    int = 0

    def __repr__(self) -> str:
        status = "OK" if self.verified else "REJECTED"
        return (
            f"<VerificationResult [{status}] "
            f"conf={self.confidence:.2f} article_id={self.article_id}>"
        )


# ── Клиент Perplexity (OpenAI-совместимый) ────────────────────────────────────

def _get_client() -> openai.AsyncOpenAI:
    """Создаёт клиент Perplexity с OpenAI-совместимым API."""
    return openai.AsyncOpenAI(
        api_key=settings.PERPLEXITY_API_KEY,
        base_url="https://api.perplexity.ai",
    )


# ── Промпт ────────────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = (
    "Ты — ассистент по проверке фактов. "
    "Отвечай ТОЛЬКО валидным JSON без пояснений вокруг него."
)

def _build_user_prompt(article: RawArticleCandidate) -> str:
    return f"""Проверь эту новостную статью. Найди 2-3 независимых источника, подтверждающих или опровергающих информацию.

Заголовок: {article.title}
Краткое содержание: {article.content[:MAX_CONTENT_CHARS]}
URL источника: {article.url}

Ответь СТРОГО в этом формате JSON (без текста вокруг):
{{
    "verified": true или false,
    "confidence": число от 0.0 до 1.0,
    "reasoning": "краткое объяснение на русском языке",
    "sources": ["url1", "url2"],
    "is_duplicate_topic": true или false
}}"""


# ── Retry-декоратор для API-вызовов ──────────────────────────────────────────

def _retryable(func):
    """
    3 попытки с экспоненциальной задержкой.
    Срабатывает только на RateLimitError и APIStatusError — остальные исключения
    пробрасываются немедленно.
    """
    return retry(
        retry=retry_if_exception_type(
            (openai.RateLimitError, openai.APIStatusError)
        ),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=4, max=30),
        reraise=True,
    )(func)


# ── Основная логика ───────────────────────────────────────────────────────────

@_retryable
async def _call_perplexity(article: RawArticleCandidate) -> dict:
    """
    Вызывает Perplexity Sonar API и возвращает спарсенный JSON-ответ.

    Raises:
        json.JSONDecodeError: если модель вернула не-JSON.
        openai.RateLimitError: при превышении лимита (tenacity повторит).
    """
    client = _get_client()
    response = await client.chat.completions.create(
        model=PERPLEXITY_MODEL,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user",   "content": _build_user_prompt(article)},
        ],
        temperature=0.1,   # Низкая температура — стабильный JSON
        max_tokens=512,
    )

    raw_content = response.choices[0].message.content.strip()

    # Perplexity иногда оборачивает JSON в markdown-блок ```json ... ```
    if raw_content.startswith("```"):
        lines = raw_content.splitlines()
        raw_content = "\n".join(
            line for line in lines
            if not line.startswith("```")
        ).strip()

    data = json.loads(raw_content)

    # Прикрепляем токены к ответу для логирования
    data["_input_tokens"]  = getattr(response.usage, "prompt_tokens",     0)
    data["_output_tokens"] = getattr(response.usage, "completion_tokens", 0)

    return data


async def verify(article: RawArticleCandidate) -> VerificationResult:
    """
    Верифицирует статью через Perplexity Sonar.

    Применяет правила отклонения и возвращает VerificationResult
    с полным набором метаданных для логирования.

    Args:
        article: Кандидат от агента Researcher.

    Returns:
        VerificationResult с verified=True если статья прошла все проверки.
    """
    t0 = time.monotonic()
    logger.info(f"[fact_checker] Верификация: {article.title[:70]!r}")

    # --- Защита от пустого API-ключа ---
    if not settings.PERPLEXITY_API_KEY:
        logger.warning("[fact_checker] PERPLEXITY_API_KEY не задан — пропуск верификации")
        return VerificationResult(
            article_id=article.db_id,
            verified=False,
            confidence=0.0,
            reason="PERPLEXITY_API_KEY не задан",
        )

    try:
        data = await _call_perplexity(article)
    except json.JSONDecodeError as exc:
        logger.error(f"[fact_checker] Не удалось спарсить JSON: {exc}")
        latency = int((time.monotonic() - t0) * 1000)
        return VerificationResult(
            article_id=article.db_id,
            verified=False,
            confidence=0.0,
            reason=f"JSON parse error: {exc}",
            latency_ms=latency,
        )
    except Exception as exc:
        logger.error(f"[fact_checker] Ошибка API: {exc}")
        latency = int((time.monotonic() - t0) * 1000)
        return VerificationResult(
            article_id=article.db_id,
            verified=False,
            confidence=0.0,
            reason=f"API error: {exc}",
            latency_ms=latency,
        )

    latency = int((time.monotonic() - t0) * 1000)

    verified         = bool(data.get("verified", False))
    confidence       = float(data.get("confidence", 0.0))
    reasoning        = str(data.get("reasoning", ""))
    sources          = data.get("sources", [])
    is_dup           = bool(data.get("is_duplicate_topic", False))
    input_tokens     = int(data.get("_input_tokens",  0))
    output_tokens    = int(data.get("_output_tokens", 0))

    # --- Правила отклонения (порядок важен) ---
    if is_dup:
        reason = "Дубликат темы (is_duplicate_topic=true)"
        logger.warning(f"[fact_checker] REJECT: {reason}")
        return VerificationResult(
            article_id=article.db_id,
            verified=False,
            confidence=confidence,
            reason=reason,
            sources=sources,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=latency,
        )

    if confidence < MIN_CONFIDENCE:
        reason = f"Низкая достоверность: {confidence:.2f} < {MIN_CONFIDENCE}"
        logger.warning(f"[fact_checker] REJECT: {reason}")
        return VerificationResult(
            article_id=article.db_id,
            verified=False,
            confidence=confidence,
            reason=reason,
            sources=sources,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=latency,
        )

    if not verified:
        reason = reasoning or "Статья не подтверждена поиском"
        logger.warning(f"[fact_checker] REJECT: {reason[:100]}")
        return VerificationResult(
            article_id=article.db_id,
            verified=False,
            confidence=confidence,
            reason=reason,
            sources=sources,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=latency,
        )

    # --- Статья прошла все проверки ---
    logger.info(
        f"[fact_checker] OK: conf={confidence:.2f} "
        f"sources={len(sources)} latency={latency}ms "
        f"tokens={input_tokens}+{output_tokens}"
    )
    return VerificationResult(
        article_id=article.db_id,
        verified=True,
        confidence=confidence,
        reason=reasoning,
        sources=sources,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        latency_ms=latency,
    )
