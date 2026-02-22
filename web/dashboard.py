"""
API endpoints для Chart.js дашборда — web/dashboard.py.

Все endpoints требуют HTTP Basic Auth и возвращают JSON для Chart.js.
Маршруты подключаются к FastAPI-приложению из web/admin.py.
"""

import logging
from typing import List

from fastapi import Depends
from sqlalchemy import text

from db.database import async_session_factory
from web.admin import app, verify_credentials

logger = logging.getLogger(__name__)


# ── Воронка агентов ───────────────────────────────────────────────────────────

@app.get("/api/dashboard/funnel")
async def funnel(_=Depends(verify_credentials)):
    """
    Статистика по агентам: сколько OK, отклонено, ошибок, средняя задержка.

    Используется для Bar chart "Воронка агентов".
    """
    async with async_session_factory() as session:
        rows = (await session.execute(text("""
            SELECT
                agent_name,
                COUNT(*) AS total,
                SUM(CASE WHEN status = 'ok'       THEN 1 ELSE 0 END) AS ok_count,
                SUM(CASE WHEN status = 'rejected' THEN 1 ELSE 0 END) AS rejected_count,
                SUM(CASE WHEN status = 'error'    THEN 1 ELSE 0 END) AS error_count,
                CAST(AVG(latency_ms) AS INTEGER)                      AS avg_latency_ms
            FROM agent_logs
            GROUP BY agent_name
            ORDER BY CASE agent_name
                WHEN 'researcher'   THEN 1
                WHEN 'fact_checker' THEN 2
                WHEN 'writer'       THEN 3
                WHEN 'formatter'    THEN 4
                WHEN 'analyst'      THEN 5
                ELSE 6
            END
        """))).fetchall()

    return [
        {
            "agent":         r[0],
            "total":         r[1],
            "ok":            r[2],
            "rejected":      r[3],
            "errors":        r[4],
            "avg_latency_ms": r[5],
        }
        for r in rows
    ]


# ── Источники ─────────────────────────────────────────────────────────────────

@app.get("/api/dashboard/sources")
async def sources(_=Depends(verify_credentials)):
    """
    Статистика по RSS-источникам: сколько статей опубликовано из каждого.

    Используется для Horizontal bar chart "Топ источников".
    """
    async with async_session_factory() as session:
        rows = (await session.execute(text("""
            SELECT
                s.name,
                s.fetch_count,
                s.category,
                COUNT(pp.id) AS published_count
            FROM sources s
            LEFT JOIN raw_articles ra ON ra.source_id = s.id
            LEFT JOIN published_posts pp ON pp.article_id = ra.id
            GROUP BY s.id
            ORDER BY published_count DESC, s.fetch_count DESC
        """))).fetchall()

    return [
        {
            "name":            r[0],
            "fetch_count":     r[1],
            "category":        r[2],
            "published_count": r[3],
        }
        for r in rows
    ]


# ── Временная шкала публикаций ────────────────────────────────────────────────

@app.get("/api/dashboard/timeline")
async def timeline(_=Depends(verify_credentials)):
    """
    Публикации по дням за последние 30 дней.

    Используется для Line chart "График публикаций".
    """
    async with async_session_factory() as session:
        rows = (await session.execute(text("""
            SELECT
                DATE(pr.started_at)        AS day,
                SUM(pr.articles_found)     AS found,
                SUM(pr.articles_published) AS published
            FROM pipeline_runs pr
            WHERE pr.started_at > datetime('now', '-30 days')
              AND pr.status IN ('completed', 'completed_empty')
            GROUP BY DATE(pr.started_at)
            ORDER BY day ASC
        """))).fetchall()

    return [
        {"day": r[0], "found": r[1], "published": r[2]}
        for r in rows
    ]


# ── Расход токенов (прокси стоимости) ────────────────────────────────────────

@app.get("/api/dashboard/costs")
async def costs(_=Depends(verify_credentials)):
    """
    Токены по агентам по дням за последние 30 дней.

    Используется для Line chart "Расход токенов / оценка стоимости".
    """
    async with async_session_factory() as session:
        rows = (await session.execute(text("""
            SELECT
                DATE(al.created_at)             AS day,
                al.agent_name,
                SUM(al.input_tokens)            AS input_tokens,
                SUM(al.output_tokens)           AS output_tokens
            FROM agent_logs al
            WHERE al.created_at > datetime('now', '-30 days')
              AND (al.input_tokens > 0 OR al.output_tokens > 0)
            GROUP BY DATE(al.created_at), al.agent_name
            ORDER BY day ASC, al.agent_name
        """))).fetchall()

    return [
        {
            "day":           r[0],
            "agent":         r[1],
            "input_tokens":  r[2],
            "output_tokens": r[3],
        }
        for r in rows
    ]


# ── Последние посты ───────────────────────────────────────────────────────────

@app.get("/api/dashboard/recent_posts")
async def recent_posts(_=Depends(verify_credentials)):
    """
    Последние 10 опубликованных постов с превью текста.

    Используется для таблицы "Последние публикации" на дашборде.
    """
    async with async_session_factory() as session:
        rows = (await session.execute(text("""
            SELECT
                pp.id,
                pp.published_at,
                pp.source_name,
                pp.telegram_msg_id,
                pp.has_image,
                pp.channel_id,
                SUBSTR(pp.post_text, 1, 120) AS preview
            FROM published_posts pp
            ORDER BY pp.published_at DESC
            LIMIT 10
        """))).fetchall()

    return [
        {
            "id":              r[0],
            "published_at":    str(r[1]),
            "source_name":     r[2],
            "telegram_msg_id": r[3],
            "has_image":       bool(r[4]),
            "channel_id":      r[5],
            "preview":         r[6],
        }
        for r in rows
    ]


# ── Ручной запуск пайплайна ───────────────────────────────────────────────────

@app.post("/api/pipeline/run")
async def manual_run(is_morning: bool = False, _=Depends(verify_credentials)):
    """
    Запускает пайплайн вручную (из дашборда или через API).

    Параметр is_morning=true запускает режим дайджеста (утренний прогон).
    Создаёт прогон и запускает его в фоне через asyncio.create_task.
    Возвращает run_id немедленно.
    """
    import asyncio
    from core.pipeline import create_pipeline_run, run_pipeline

    run_id = await create_pipeline_run()
    mode = "digest" if is_morning else "single"

    async def _run():
        try:
            await run_pipeline(run_id, is_morning=is_morning)
        except Exception as exc:
            logger.error(f"[dashboard] Ошибка ручного запуска: {exc}")

    asyncio.create_task(_run())
    return {"run_id": run_id, "status": "started", "mode": mode}
