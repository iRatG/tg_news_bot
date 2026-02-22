"""
Семантическая дедупликация статей.

Использует DeepSeek embeddings API для генерации векторов
и косинусное сходство для обнаружения дубликатов по смыслу.

Алгоритм:
    1. Генерируем embedding для текста новой статьи (title + content[:512]).
    2. Загружаем векторы опубликованных статей за последние N дней (макс. 200).
    3. Вычисляем максимальное косинусное сходство через numpy.
    4. Если max_sim > порога — статья считается дубликатом.
    5. После публикации вызываем save_embedding() для сохранения вектора.

Примечание: DeepSeek embeddings API (если недоступен) — возвращает 0.0 gracefully,
только URL-based dedup продолжает работать.

RAM: 200 векторов × 1536 dim × 4 байта ≈ 1.2 MB — безопасно для 1GB VPS.
"""

import json
import logging
from typing import List, Optional

import numpy as np
import openai
from sqlalchemy import text

from core.config import settings
from db.database import async_session_factory

logger = logging.getLogger(__name__)

# ── Константы ─────────────────────────────────────────────────────────────────

EMBEDDING_MODEL    = "deepseek-chat"   # DeepSeek не публикует отдельную модель embeddings — возвращает 0.0 gracefully
EMBEDDING_DIMS     = 1536
MAX_INPUT_CHARS    = 512   # Ограничиваем вход для экономии токенов
MAX_VECTORS_LOADED = 200   # Защита памяти на 1GB VPS


# ── Внутренние утилиты ────────────────────────────────────────────────────────

def _get_client() -> openai.AsyncOpenAI:
    """Создаёт асинхронный DeepSeek-клиент (OpenAI-совместимый) для работы с embeddings."""
    return openai.AsyncOpenAI(
        api_key=settings.DEEPSEEK_API_KEY,
        base_url="https://api.deepseek.com",
    )


def _cosine_similarity(a: List[float], b: List[float]) -> float:
    """
    Вычисляет косинусное сходство между двумя векторами.

    Returns:
        Значение от 0.0 (полная разница) до 1.0 (идентичные векторы).
    """
    va = np.array(a, dtype=np.float32)
    vb = np.array(b, dtype=np.float32)
    norm_a = np.linalg.norm(va)
    norm_b = np.linalg.norm(vb)
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return float(np.dot(va, vb) / (norm_a * norm_b))


async def _generate_embedding(text_input: str) -> Optional[List[float]]:
    """
    Генерирует embedding через OpenAI API.

    Args:
        text_input: Текст (будет обрезан до MAX_INPUT_CHARS).

    Returns:
        Список из EMBEDDING_DIMS float-значений или None при ошибке.
    """
    if not settings.DEEPSEEK_API_KEY:
        logger.warning("[dedup] DEEPSEEK_API_KEY не задан — embedding пропущен")
        return None

    client = _get_client()
    try:
        response = await client.embeddings.create(
            model=EMBEDDING_MODEL,
            input=text_input[:MAX_INPUT_CHARS],
        )
        return response.data[0].embedding
    except Exception as exc:
        logger.warning(f"[dedup] DeepSeek embedding недоступен (graceful skip): {type(exc).__name__}")
        return None


# ── Публичный интерфейс ───────────────────────────────────────────────────────

async def check_similarity(
    text_input: str,
    lookback_days: int = 30,
) -> float:
    """
    Вычисляет максимальное косинусное сходство статьи с опубликованными.

    Загружает векторы из article_embeddings за последние lookback_days дней,
    сравнивает с embedding нового текста и возвращает наибольшее значение.

    Args:
        text_input:    Текст для проверки (title + content[:200] рекомендуется).
        lookback_days: Глубина поиска дубликатов в днях.

    Returns:
        Максимальное косинусное сходство в диапазоне [0.0, 1.0].
        Возвращает 0.0 если опубликованных статей нет или embedding недоступен.
    """
    # Генерируем вектор нового текста
    query_vector = await _generate_embedding(text_input)
    if query_vector is None:
        return 0.0

    # Загружаем векторы опубликованных статей за период
    async with async_session_factory() as session:
        rows = (await session.execute(
            text("""
                SELECT ae.embedding
                FROM article_embeddings ae
                JOIN raw_articles ra ON ae.article_id = ra.id
                WHERE ra.status = 'published'
                  AND ra.fetched_at > datetime('now', :delta)
                ORDER BY ra.fetched_at DESC
                LIMIT :limit
            """),
            {
                "delta":  f"-{lookback_days} days",
                "limit":  MAX_VECTORS_LOADED,
            },
        )).fetchall()

    if not rows:
        logger.debug("[dedup] Нет опубликованных статей для сравнения")
        return 0.0

    # Вычисляем максимальное сходство
    max_sim = 0.0
    for (embedding_json,) in rows:
        try:
            stored_vector = json.loads(embedding_json)
            sim = _cosine_similarity(query_vector, stored_vector)
            if sim > max_sim:
                max_sim = sim
        except (json.JSONDecodeError, ValueError) as exc:
            logger.debug(f"[dedup] Не удалось прочитать вектор: {exc}")
            continue

    logger.debug(
        f"[dedup] Сравнение с {len(rows)} векторами: max_sim={max_sim:.4f}"
    )
    return max_sim


async def save_embedding(article_id: int, text_input: str) -> bool:
    """
    Сохраняет embedding статьи после успешной публикации.

    Вызывается агентом Analyst сразу после публикации поста.
    Вектор сохраняется в article_embeddings для будущих проверок.

    Args:
        article_id: ID записи в raw_articles.
        text_input: Текст (title + content[:200]).

    Returns:
        True если embedding успешно сохранён, False при ошибке.
    """
    vector = await _generate_embedding(text_input)
    if vector is None:
        return False

    async with async_session_factory() as session:
        try:
            await session.execute(
                text(
                    "INSERT OR IGNORE INTO article_embeddings "
                    "(article_id, embedding) "
                    "VALUES (:article_id, :embedding)"
                ),
                {
                    "article_id": article_id,
                    "embedding":  json.dumps(vector),
                },
            )
            await session.commit()
            logger.info(f"[dedup] Embedding сохранён для article_id={article_id}")
            return True
        except Exception as exc:
            logger.error(f"[dedup] Ошибка сохранения embedding: {exc}")
            return False
