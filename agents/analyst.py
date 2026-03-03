"""
Агент 5 — Analyst / Publisher.

Финальный gate-keeper пайплайна: проверяет качество, выполняет
семантическую дедупликацию и принимает решение о публикации.

Алгоритм (fail-fast, порядок проверок важен):
    1. Целостность пайплайна — все предыдущие агенты отработали.
    2. Качество контента — длина, наличие URL, эмодзи.
    3. Семантическая дедупликация — cosine similarity < порога из settings.
    4. Жёсткая URL-дедупликация — страховка от точных дубликатов.
    5. Публикация в Telegram + сохранение embedding + запись в published_posts.

Если 0 публикаций за прогон — уведомляет администратора.
Стоимость: ~$0.001/месяц (только embeddings при проверке дедупликации).
"""

import logging
import time
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import text

from agents.fact_checker import VerificationResult
from agents.formatter import FormatterResult
from agents.researcher import RawArticleCandidate
from agents.writer import WriterResult
from core.config import get_setting, settings
from core.dedup import check_similarity, save_embedding
from core.publisher import notify_admin, publish_post
from db.database import async_session_factory
from db.models import ArticleStatus

logger = logging.getLogger(__name__)


# ── Выходная структура ────────────────────────────────────────────────────────

@dataclass
class AnalystResult:
    """Результат финальной проверки и решение о публикации."""

    article_id:      int
    published:       bool
    telegram_msg_id: Optional[int] = None
    reason:          Optional[str] = None

    def __repr__(self) -> str:
        status = f"PUBLISHED msg_id={self.telegram_msg_id}" if self.published else f"REJECTED: {self.reason}"
        return f"<AnalystResult [{status}] article_id={self.article_id}>"


# ── Вспомогательные функции ───────────────────────────────────────────────────

async def _is_url_published(url: str) -> bool:
    """Проверяет что URL ещё не опубликован (жёсткая дедупликация)."""
    async with async_session_factory() as session:
        result = await session.execute(
            text(
                "SELECT id FROM raw_articles "
                "WHERE url = :url AND status = :status"
            ),
            {"url": url, "status": ArticleStatus.PUBLISHED},
        )
        return result.fetchone() is not None


async def _save_published_post(
    article: RawArticleCandidate,
    formatter_result: FormatterResult,
    run_id: int,
    telegram_msg_id: int,
) -> None:
    """Записывает опубликованный пост в таблицу published_posts."""
    async with async_session_factory() as session:
        await session.execute(
            text("""
                INSERT INTO published_posts
                    (article_id, run_id, telegram_msg_id, channel_id,
                     post_text, source_url, source_name, has_image)
                VALUES
                    (:article_id, :run_id, :telegram_msg_id, :channel_id,
                     :post_text, :source_url, :source_name, :has_image)
            """),
            {
                "article_id":      article.db_id,
                "run_id":          run_id,
                "telegram_msg_id": telegram_msg_id,
                "channel_id":      settings.TELEGRAM_CHANNEL_ID,
                "post_text":       formatter_result.formatted_text,
                "source_url":      article.url,
                "source_name":     article.source_name,
                "has_image":       1 if formatter_result.image_bytes else 0,
            },
        )
        await session.commit()


async def _set_article_status(article_id: int, status: ArticleStatus) -> None:
    """Обновляет статус статьи в raw_articles."""
    async with async_session_factory() as session:
        await session.execute(
            text("UPDATE raw_articles SET status = :status WHERE id = :id"),
            {"status": status, "id": article_id},
        )
        await session.commit()


# ── Публичный интерфейс ───────────────────────────────────────────────────────

async def evaluate_and_publish(
    article:          RawArticleCandidate,
    verification:     VerificationResult,
    writer_result:    WriterResult,
    formatter_result: FormatterResult,
    run_id:           int,
) -> AnalystResult:
    """
    Финальная проверка и публикация поста.

    Применяет все качественные и дедупликационные проверки.
    При успехе: публикует в Telegram, сохраняет embedding, обновляет БД.
    При отказе: помечает статью как REJECTED, логирует причину.

    Args:
        article:          Кандидат от Researcher.
        verification:     Результат Fact-Checker.
        writer_result:    Результат Writer.
        formatter_result: Результат Formatter.
        run_id:           ID текущего прогона пайплайна.

    Returns:
        AnalystResult с результатом публикации.
    """
    t0 = time.monotonic()
    logger.info(f"[analyst] Проверка article_id={article.db_id}: {article.title[:60]!r}")

    post_text = formatter_result.formatted_text

    # ── Проверка 1: Целостность пайплайна ─────────────────────────────────────
    if not verification.verified:
        reason = f"Fact-checker: {verification.reason}"
        await _set_article_status(article.db_id, ArticleStatus.REJECTED)
        logger.warning(f"[analyst] REJECT (pipeline integrity): {reason}")
        return AnalystResult(article_id=article.db_id, published=False, reason=reason)

    # ── Проверка 2: Качество контента ─────────────────────────────────────────
    char_count = len(post_text)
    if char_count < 300:
        reason = f"Пост слишком короткий: {char_count} симв. (мин. 300)"
        await _set_article_status(article.db_id, ArticleStatus.REJECTED)
        logger.warning(f"[analyst] REJECT (quality): {reason}")
        return AnalystResult(article_id=article.db_id, published=False, reason=reason)

    # Лимит зависит от формата: analysis/longread/digest — 4096, brief/single — 1024
    max_len = 4096 if formatter_result.post_format in ("analysis", "longread", "digest") else 1024
    if char_count > max_len:
        reason = f"Пост превышает лимит: {char_count} симв. (макс. {max_len})"
        await _set_article_status(article.db_id, ArticleStatus.REJECTED)
        logger.warning(f"[analyst] REJECT (quality): {reason}")
        return AnalystResult(article_id=article.db_id, published=False, reason=reason)

    if "http" not in post_text:
        reason = "В посте отсутствует ссылка на источник"
        await _set_article_status(article.db_id, ArticleStatus.REJECTED)
        logger.warning(f"[analyst] REJECT (quality): {reason}")
        return AnalystResult(article_id=article.db_id, published=False, reason=reason)

    # ── Проверка 3: Семантическая дедупликация ────────────────────────────────
    dedup_threshold = float(await get_setting("dedup_threshold", "0.80"))
    lookback_days   = int(await get_setting("dedup_lookback_days", "30"))

    dedup_text  = f"{article.title} {article.content[:200]}"
    similarity  = await check_similarity(dedup_text, lookback_days=lookback_days)

    if similarity >= dedup_threshold:
        reason = f"Семантический дубликат: similarity={similarity:.3f} >= {dedup_threshold}"
        await _set_article_status(article.db_id, ArticleStatus.REJECTED)
        logger.warning(f"[analyst] REJECT (dedup): {reason}")
        return AnalystResult(article_id=article.db_id, published=False, reason=reason)

    # ── Проверка 4: Жёсткая URL-дедупликация (страховка) ─────────────────────
    if await _is_url_published(article.url):
        reason = f"URL уже опубликован: {article.url}"
        await _set_article_status(article.db_id, ArticleStatus.REJECTED)
        logger.warning(f"[analyst] REJECT (url dedup): {reason}")
        return AnalystResult(article_id=article.db_id, published=False, reason=reason)

    # ── Публикация ────────────────────────────────────────────────────────────
    logger.info(
        f"[analyst] Все проверки пройдены. "
        f"similarity={similarity:.3f} | Публикуем в {settings.TELEGRAM_CHANNEL_ID}..."
    )

    try:
        telegram_msg_id = await publish_post(formatter_result)
    except Exception as exc:
        reason = f"Ошибка публикации в Telegram: {exc}"
        await _set_article_status(article.db_id, ArticleStatus.FAILED)
        logger.error(f"[analyst] {reason}")
        return AnalystResult(article_id=article.db_id, published=False, reason=reason)

    # ── Пост-публикация: сохраняем данные ────────────────────────────────────
    await _set_article_status(article.db_id, ArticleStatus.PUBLISHED)
    await _save_published_post(article, formatter_result, run_id, telegram_msg_id)

    # Сохраняем embedding для будущей дедупликации
    await save_embedding(article.db_id, dedup_text)

    latency = int((time.monotonic() - t0) * 1000)
    logger.info(
        f"[analyst] PUBLISHED: msg_id={telegram_msg_id} | "
        f"{char_count} симв. | {latency}мс"
    )

    return AnalystResult(
        article_id=article.db_id,
        published=True,
        telegram_msg_id=telegram_msg_id,
        reason=None,
    )
