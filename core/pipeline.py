"""
Оркестратор пайплайна — core/pipeline.py.

Запускает агентов последовательно. Поддерживает два режима:

ОДИНОЧНЫЙ (is_morning=False, по умолчанию):
    Researcher → [top-5 кандидатов]
        ↓ (для каждого до posts_per_run)
    Fact-Checker → Writer → Formatter → Analyst

ДАЙДЖЕСТ (is_morning=True):
    Researcher → [все кандидаты]
        ↓ (верифицируем всех параллельно)
    Fact-Checker × N → write_digest(все верифицированные)
        ↓ (один раз)
    Formatter → Analyst → публикация одного поста

В режиме дайджеста все верифицированные статьи помечаются как PUBLISHED.
"""

import logging
import time
from typing import List, Tuple

from sqlalchemy import text

from agents import analyst, fact_checker, formatter, researcher, writer
from agents.fact_checker import VerificationResult
from agents.researcher import RawArticleCandidate
from core.config import get_setting
from core.logger import agent_logger
from core.publisher import notify_admin
from db.database import async_session_factory
from db.models import ArticleStatus, RunStatus

logger = logging.getLogger(__name__)


# ── Вспомогательные функции работы с БД ──────────────────────────────────────

async def create_pipeline_run() -> int:
    """
    Создаёт запись о новом прогоне в pipeline_runs.

    Returns:
        ID созданного прогона.
    """
    async with async_session_factory() as session:
        result = await session.execute(
            text(
                "INSERT INTO pipeline_runs (status) VALUES (:status)"
            ),
            {"status": RunStatus.RUNNING},
        )
        await session.commit()
        return result.lastrowid


async def _finish_run(
    run_id:            int,
    articles_found:    int,
    articles_verified: int,
    articles_published: int,
    status:            RunStatus,
    error_message:     str = None,
) -> None:
    """Закрывает прогон с финальным статусом и статистикой."""
    async with async_session_factory() as session:
        await session.execute(
            text("""
                UPDATE pipeline_runs
                SET finished_at         = datetime('now'),
                    articles_found      = :found,
                    articles_verified   = :verified,
                    articles_published  = :published,
                    status              = :status,
                    error_message       = :error
                WHERE id = :id
            """),
            {
                "found":    articles_found,
                "verified": articles_verified,
                "published": articles_published,
                "status":   status,
                "error":    error_message,
                "id":       run_id,
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


# ── Основная логика пайплайна ─────────────────────────────────────────────────

async def run_pipeline(run_id: int, is_morning: bool = False) -> None:
    """
    Выполняет полный цикл обработки новостей для одного прогона.

    Args:
        run_id:     ID прогона в таблице pipeline_runs.
        is_morning: Если True — режим утреннего дайджеста (все статьи в один пост).
    """
    t_start = time.monotonic()
    mode_str = "ДАЙДЖЕСТ" if is_morning else "ОДИНОЧНЫЙ"
    logger.info(f"[pipeline] === ПРОГОН #{run_id} НАЧАТ ({mode_str}) ===")

    try:
        # ── Агент 1: Researcher ───────────────────────────────────────────────
        candidates = await researcher.fetch_and_rank()

        await agent_logger.log_agent(
            agent_name="researcher",
            run_id=run_id,
            article_id=None,
            status="ok",
            reason=f"Найдено кандидатов: {len(candidates)}",
        )

        if not candidates:
            logger.warning(f"[pipeline] #{run_id}: Нет кандидатов из RSS")
            await _finish_run(run_id, 0, 0, 0, RunStatus.COMPLETED_EMPTY)
            await notify_admin(
                f"⚠️ Прогон #{run_id}: Нет кандидатов из RSS\n"
                f"Проверьте доступность источников в /admin"
            )
            return

        # ── РЕЖИМ ДАЙДЖЕСТА ───────────────────────────────────────────────────
        if is_morning:
            await _run_digest(run_id, candidates, t_start)
            return

        published_count = 0
        verified_count  = 0

        # ── РЕЖИМ ОДИНОЧНОГО ПОСТА ────────────────────────────────────────────
        posts_per_run = int(await get_setting("posts_per_run", "1"))

        for candidate in candidates:
            if published_count >= posts_per_run:
                break

            await _set_article_status(candidate.db_id, ArticleStatus.PROCESSING)
            logger.info(
                f"[pipeline] #{run_id}: Обработка [{candidate.score}] "
                f"{candidate.title[:60]!r}"
            )

            # ── Агент 2: Fact-Checker ─────────────────────────────────────────
            t2 = time.monotonic()
            try:
                verification = await fact_checker.verify(candidate)
            except Exception as exc:
                latency = int((time.monotonic() - t2) * 1000)
                await agent_logger.log_agent(
                    "fact_checker", run_id, candidate.db_id,
                    "error", reason=str(exc), latency_ms=latency,
                )
                await _set_article_status(candidate.db_id, ArticleStatus.FAILED)
                continue

            latency = int((time.monotonic() - t2) * 1000)
            fc_status = "ok" if verification.verified else "rejected"
            await agent_logger.log_agent(
                "fact_checker", run_id, candidate.db_id, fc_status,
                reason=verification.reason,
                input_tokens=verification.input_tokens,
                output_tokens=verification.output_tokens,
                latency_ms=latency,
            )

            if not verification.verified:
                await _set_article_status(candidate.db_id, ArticleStatus.REJECTED)
                continue

            verified_count += 1

            # ── Агент 3: Writer ───────────────────────────────────────────────
            t3 = time.monotonic()
            try:
                writer_result = await writer.write_post(candidate, verification)
            except Exception as exc:
                latency = int((time.monotonic() - t3) * 1000)
                await agent_logger.log_agent(
                    "writer", run_id, candidate.db_id,
                    "error", reason=str(exc), latency_ms=latency,
                )
                await _set_article_status(candidate.db_id, ArticleStatus.FAILED)
                continue

            latency = int((time.monotonic() - t3) * 1000)
            await agent_logger.log_agent(
                "writer", run_id, candidate.db_id, "ok",
                reason=f"{writer_result.char_count} симв. format={writer_result.post_format}",
                input_tokens=writer_result.input_tokens,
                output_tokens=writer_result.output_tokens,
                latency_ms=latency,
            )

            # ── Агент 4: Formatter ────────────────────────────────────────────
            t4 = time.monotonic()
            try:
                formatter_result = await formatter.format_post(writer_result)
            except Exception as exc:
                latency = int((time.monotonic() - t4) * 1000)
                await agent_logger.log_agent(
                    "formatter", run_id, candidate.db_id,
                    "error", reason=str(exc), latency_ms=latency,
                )
                await _set_article_status(candidate.db_id, ArticleStatus.FAILED)
                continue

            latency = int((time.monotonic() - t4) * 1000)
            await agent_logger.log_agent(
                "formatter", run_id, candidate.db_id, "ok",
                reason=f"image={'да' if formatter_result.image_bytes else 'нет'}",
                input_tokens=formatter_result.input_tokens,
                output_tokens=formatter_result.output_tokens,
                latency_ms=latency,
            )

            # ── Агент 5: Analyst / Publisher ──────────────────────────────────
            t5 = time.monotonic()
            try:
                analyst_result = await analyst.evaluate_and_publish(
                    candidate, verification, writer_result, formatter_result, run_id,
                )
            except Exception as exc:
                latency = int((time.monotonic() - t5) * 1000)
                await agent_logger.log_agent(
                    "analyst", run_id, candidate.db_id,
                    "error", reason=str(exc), latency_ms=latency,
                )
                await _set_article_status(candidate.db_id, ArticleStatus.FAILED)
                continue

            latency = int((time.monotonic() - t5) * 1000)
            a_status = "ok" if analyst_result.published else "rejected"
            await agent_logger.log_agent(
                "analyst", run_id, candidate.db_id, a_status,
                reason=analyst_result.reason,
                latency_ms=latency,
            )

            if analyst_result.published:
                published_count += 1
                logger.info(
                    f"[pipeline] #{run_id}: ✓ Опубликовано "
                    f"msg_id={analyst_result.telegram_msg_id}"
                )

        # ── Финализация прогона ───────────────────────────────────────────────
        final_status = (
            RunStatus.COMPLETED if published_count > 0
            else RunStatus.COMPLETED_EMPTY
        )
        elapsed = int((time.monotonic() - t_start) * 1000)

        await _finish_run(
            run_id=run_id,
            articles_found=len(candidates),
            articles_verified=verified_count,
            articles_published=published_count,
            status=final_status,
        )

        logger.info(
            f"[pipeline] === ПРОГОН #{run_id} ЗАВЕРШЁН: "
            f"найдено={len(candidates)} верифицировано={verified_count} "
            f"опубликовано={published_count} | {elapsed}мс ==="
        )

        if published_count == 0:
            await notify_admin(
                f"⚠️ Прогон #{run_id}: 0 постов опубликовано\n"
                f"Кандидатов: {len(candidates)}, верифицировано: {verified_count}\n"
                f"Детали: /admin → Agent Logs"
            )

    except Exception as exc:
        elapsed = int((time.monotonic() - t_start) * 1000)
        logger.error(f"[pipeline] #{run_id}: СИСТЕМНАЯ ОШИБКА: {exc}", exc_info=True)
        await _finish_run(run_id, 0, 0, 0, RunStatus.FAILED, str(exc))
        await notify_admin(
            f"🔴 Прогон #{run_id} УПАЛ с ошибкой:\n"
            f"<code>{str(exc)[:300]}</code>"
        )
        raise


async def _run_digest(
    run_id: int,
    candidates: List[RawArticleCandidate],
    t_start: float,
) -> None:
    """
    Внутренняя логика утреннего дайджеста.

    Верифицирует всех кандидатов, пишет один общий пост,
    публикует через analyst (primary=первая статья),
    помечает все остальные статьи как PUBLISHED.
    """
    published_count = 0
    verified_count  = 0

    # ── Шаг 1: Верификация всех кандидатов ───────────────────────────────────
    verified_pairs: List[Tuple[RawArticleCandidate, VerificationResult]] = []

    for candidate in candidates:
        await _set_article_status(candidate.db_id, ArticleStatus.PROCESSING)
        logger.info(
            f"[pipeline] #{run_id} [digest] Верификация [{candidate.score}] "
            f"{candidate.title[:60]!r}"
        )

        t2 = time.monotonic()
        try:
            verification = await fact_checker.verify(candidate)
        except Exception as exc:
            latency = int((time.monotonic() - t2) * 1000)
            await agent_logger.log_agent(
                "fact_checker", run_id, candidate.db_id,
                "error", reason=str(exc), latency_ms=latency,
            )
            await _set_article_status(candidate.db_id, ArticleStatus.FAILED)
            continue

        latency = int((time.monotonic() - t2) * 1000)
        fc_status = "ok" if verification.verified else "rejected"
        await agent_logger.log_agent(
            "fact_checker", run_id, candidate.db_id, fc_status,
            reason=verification.reason,
            input_tokens=verification.input_tokens,
            output_tokens=verification.output_tokens,
            latency_ms=latency,
        )

        if verification.verified:
            verified_pairs.append((candidate, verification))
            verified_count += 1
        else:
            await _set_article_status(candidate.db_id, ArticleStatus.REJECTED)

    if not verified_pairs:
        logger.warning(f"[pipeline] #{run_id} [digest]: Нет верифицированных статей")
        elapsed = int((time.monotonic() - t_start) * 1000)
        await _finish_run(run_id, len(candidates), 0, 0, RunStatus.COMPLETED_EMPTY)
        await notify_admin(
            f"⚠️ Прогон #{run_id} (дайджест): 0 статей прошло верификацию\n"
            f"Кандидатов: {len(candidates)}\nДетали: /admin → Agent Logs"
        )
        return

    primary_candidate, primary_verification = verified_pairs[0]

    # ── Шаг 2: Написание дайджеста ────────────────────────────────────────────
    t3 = time.monotonic()
    try:
        writer_result = await writer.write_digest(verified_pairs)
    except Exception as exc:
        latency = int((time.monotonic() - t3) * 1000)
        await agent_logger.log_agent(
            "writer", run_id, primary_candidate.db_id,
            "error", reason=str(exc), latency_ms=latency,
        )
        await _set_article_status(primary_candidate.db_id, ArticleStatus.FAILED)
        elapsed = int((time.monotonic() - t_start) * 1000)
        await _finish_run(run_id, len(candidates), verified_count, 0, RunStatus.FAILED, str(exc))
        return

    latency = int((time.monotonic() - t3) * 1000)
    await agent_logger.log_agent(
        "writer", run_id, primary_candidate.db_id, "ok",
        reason=f"{writer_result.char_count} симв. format=digest n={len(verified_pairs)}",
        input_tokens=writer_result.input_tokens,
        output_tokens=writer_result.output_tokens,
        latency_ms=latency,
    )

    # ── Шаг 3: Форматирование ─────────────────────────────────────────────────
    t4 = time.monotonic()
    try:
        formatter_result = await formatter.format_post(writer_result)
    except Exception as exc:
        latency = int((time.monotonic() - t4) * 1000)
        await agent_logger.log_agent(
            "formatter", run_id, primary_candidate.db_id,
            "error", reason=str(exc), latency_ms=latency,
        )
        await _set_article_status(primary_candidate.db_id, ArticleStatus.FAILED)
        elapsed = int((time.monotonic() - t_start) * 1000)
        await _finish_run(run_id, len(candidates), verified_count, 0, RunStatus.FAILED, str(exc))
        return

    latency = int((time.monotonic() - t4) * 1000)
    await agent_logger.log_agent(
        "formatter", run_id, primary_candidate.db_id, "ok",
        reason=f"image={'да' if formatter_result.image_bytes else 'нет'}",
        input_tokens=formatter_result.input_tokens,
        output_tokens=formatter_result.output_tokens,
        latency_ms=latency,
    )

    # ── Шаг 4: Публикация (primary статья) ───────────────────────────────────
    t5 = time.monotonic()
    try:
        analyst_result = await analyst.evaluate_and_publish(
            primary_candidate, primary_verification,
            writer_result, formatter_result, run_id,
        )
    except Exception as exc:
        latency = int((time.monotonic() - t5) * 1000)
        await agent_logger.log_agent(
            "analyst", run_id, primary_candidate.db_id,
            "error", reason=str(exc), latency_ms=latency,
        )
        await _set_article_status(primary_candidate.db_id, ArticleStatus.FAILED)
        elapsed = int((time.monotonic() - t_start) * 1000)
        await _finish_run(run_id, len(candidates), verified_count, 0, RunStatus.FAILED, str(exc))
        return

    latency = int((time.monotonic() - t5) * 1000)
    a_status = "ok" if analyst_result.published else "rejected"
    await agent_logger.log_agent(
        "analyst", run_id, primary_candidate.db_id, a_status,
        reason=analyst_result.reason,
        latency_ms=latency,
    )

    if analyst_result.published:
        published_count = 1
        logger.info(
            f"[pipeline] #{run_id} [digest]: ✓ Дайджест опубликован "
            f"msg_id={analyst_result.telegram_msg_id} "
            f"({len(verified_pairs)} новостей)"
        )
        # Помечаем остальные статьи дайджеста как PUBLISHED
        for art, _ in verified_pairs[1:]:
            await _set_article_status(art.db_id, ArticleStatus.PUBLISHED)

    # ── Финализация ───────────────────────────────────────────────────────────
    final_status = RunStatus.COMPLETED if published_count > 0 else RunStatus.COMPLETED_EMPTY
    elapsed = int((time.monotonic() - t_start) * 1000)

    await _finish_run(
        run_id=run_id,
        articles_found=len(candidates),
        articles_verified=verified_count,
        articles_published=published_count,
        status=final_status,
    )

    logger.info(
        f"[pipeline] === ПРОГОН #{run_id} [ДАЙДЖЕСТ] ЗАВЕРШЁН: "
        f"найдено={len(candidates)} верифицировано={verified_count} "
        f"опубликовано={published_count} | {elapsed}мс ==="
    )

    if published_count == 0:
        await notify_admin(
            f"⚠️ Прогон #{run_id} (дайджест): 0 постов опубликовано\n"
            f"Кандидатов: {len(candidates)}, верифицировано: {verified_count}\n"
            f"Детали: /admin → Agent Logs"
        )
