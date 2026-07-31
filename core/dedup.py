"""
Дедупликация статей против нашей истории публикаций.

DeepSeek не предоставляет embeddings API (подтверждено официальной
документацией и живым тест-вызовом: 404 NotFoundError на /embeddings,
при том что chat completions с того же сервера отвечают нормально) —
поэтому вместо векторного сходства используется прямое LLM-сравнение
через deepseek-v4-flash chat completion.

Алгоритм:
    1. Загружаем последние N заголовков ОПУБЛИКОВАННЫХ нами статей
       за lookback_days дней.
    2. Просим модель явно определить (субъект, событие) кандидата и
       лучшего совпадения из списка, затем формально сравнить:
       дубликат ⇔ совпадают И субъект, И событие.
    3. thinking-режим модели отключён явно (extra_body) — структурированные
       промежуточные поля в самой JSON-схеме ответа заменяют его, сохраняя
       точность при значительно меньшем расходе токенов (проверено на
       живых данных: 4/4 тестовых кейса, включая заведомо неоднозначный).

Если DEEPSEEK_API_KEY не задан или вызов API падает — считаем "не дубликат"
(graceful skip), чтобы сбой проверки не блокировал публикацию.
"""

import json
import logging
from typing import List, Tuple

import openai
from sqlalchemy import text

from core.config import settings
from db.database import async_session_factory

logger = logging.getLogger(__name__)

# ── Константы ─────────────────────────────────────────────────────────────────

MODEL              = "deepseek-v4-flash"
MAX_PUBLISHED      = 30    # сколько последних заголовков даём модели на сравнение
MAX_TOKENS         = 400   # с запасом; фактический расход ~100-115 (thinking отключён)


# ── Промпт ────────────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = (
    "Ты — модуль дедупликации новостного Telegram-канала об ИИ. "
    "Сравни НОВУЮ статью-кандидата со списком уже ОПУБЛИКОВАННЫХ нами заголовков.\n"
    "\n"
    "Правило дедупликации (строго формальное, применяй буквально):\n"
    "Два заголовка — дубликат ТОЛЬКО если СОВПАДАЮТ ОБА признака:\n"
    "  1) ГЛАВНЫЙ СУБЪЕКТ — та же компания/организация/модель, о которой новость.\n"
    "  2) СОБЫТИЕ — то же самое конкретное действие/происшествие/релиз "
    "(не просто общая тема, а именно один и тот же факт).\n"
    "Если хотя бы один из двух признаков отличается — это НЕ дубликат, "
    "даже если тема выглядит похожей.\n"
    "НЕ считай дубликатом то, что просто 'уже писали в интернете' — сравнивай "
    "ТОЛЬКО со списком публикаций ниже.\n"
    "\n"
    "Сначала явно укажи субъект и событие кандидата и лучшего кандидата на совпадение "
    "из списка, ПОТОМ дай вердикт — так меньше шанс ошибиться.\n"
    "\n"
    "Отвечай СТРОГО JSON без текста вокруг, в этом порядке ключей:\n"
    '{"candidate_subject": "...", "candidate_event": "...", '
    '"best_match_title": "заголовок из списка или null", '
    '"best_match_subject": "... или null", "best_match_event": "... или null", '
    '"same_subject": true/false, "same_event": true/false, '
    '"is_duplicate": true/false}'
)


def _build_user_prompt(candidate_title: str, published_titles: List[str]) -> str:
    pub_list = "\n".join(f"- {t}" for t in published_titles)
    return f"Уже опубликованные заголовки:\n{pub_list}\n\nНовый кандидат:\n{candidate_title}"


def _get_client() -> openai.AsyncOpenAI:
    """Создаёт асинхронный DeepSeek-клиент (OpenAI-совместимый)."""
    return openai.AsyncOpenAI(
        api_key=settings.DEEPSEEK_API_KEY,
        base_url="https://api.deepseek.com",
    )


async def _load_recent_published_titles(lookback_days: int, limit: int) -> List[str]:
    """Загружает заголовки последних опубликованных статей за lookback_days дней."""
    async with async_session_factory() as session:
        rows = (await session.execute(
            text("""
                SELECT title FROM raw_articles
                WHERE status = 'published'
                  AND fetched_at > datetime('now', :delta)
                ORDER BY fetched_at DESC
                LIMIT :limit
            """),
            {"delta": f"-{lookback_days} days", "limit": limit},
        )).fetchall()
    return [r[0] for r in rows]


# ── Публичный интерфейс ───────────────────────────────────────────────────────

async def check_duplicate(
    title: str,
    lookback_days: int = 30,
) -> Tuple[bool, str]:
    """
    Проверяет, является ли статья дубликатом уже опубликованной нами новости.

    Args:
        title:         Заголовок статьи-кандидата.
        lookback_days: Глубина поиска дубликатов в днях.

    Returns:
        (is_duplicate, matched_title) — matched_title пустая строка, если не дубликат
        или проверка не удалась (graceful skip).
    """
    if not settings.DEEPSEEK_API_KEY:
        logger.warning("[dedup] DEEPSEEK_API_KEY не задан — проверка дубликатов пропущена")
        return False, ""

    published_titles = await _load_recent_published_titles(lookback_days, MAX_PUBLISHED)
    if not published_titles:
        return False, ""

    client = _get_client()
    try:
        response = await client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": _build_user_prompt(title, published_titles)},
            ],
            max_tokens=MAX_TOKENS,
            temperature=0.0,
            timeout=30,
            extra_body={"thinking": {"type": "disabled"}},
        )
        raw_content = response.choices[0].message.content.strip()

        if raw_content.startswith("```"):
            lines = raw_content.splitlines()
            raw_content = "\n".join(
                line for line in lines if not line.startswith("```")
            ).strip()

        data = json.loads(raw_content)
    except Exception as exc:
        logger.warning(
            f"[dedup] Проверка дубликатов недоступна (graceful skip): "
            f"{type(exc).__name__}: {exc}"
        )
        return False, ""

    is_dup  = bool(data.get("is_duplicate", False))
    matched = data.get("best_match_title") or ""

    if is_dup:
        logger.info(f"[dedup] Дубликат: {title[:60]!r} == {matched[:60]!r}")

    return is_dup, matched
