"""
Планировщик задач — core/scheduler.py.

Использует APScheduler AsyncIOScheduler с SQLite jobstore для персистентности.
Расписание публикаций управляется через таблицу schedule_slots в БД —
изменения применяются без перезапуска контейнера (через reload_schedule()).

Архитектурные решения:
    - AsyncIOScheduler: разделяет один event loop с FastAPI и агентами
    - SQLAlchemyJobStore: хранит задачи в синхронном SQLite (требование APScheduler)
    - misfire_grace_time=300: запускает пропущенные задачи в течение 5 минут
    - Timezone: Europe/Moscow (все слоты расписания — в МСК)
"""

import logging

from apscheduler.executors.asyncio import AsyncIOExecutor
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import text

from core.config import settings
from db.database import async_session_factory

logger = logging.getLogger(__name__)

# ── Инициализация планировщика ────────────────────────────────────────────────

# APScheduler требует синхронный SQLite URL для jobstore
_sync_db_url = settings.DATABASE_URL.replace("+aiosqlite", "")

scheduler = AsyncIOScheduler(
    jobstores={
        "default": SQLAlchemyJobStore(url=_sync_db_url),
    },
    executors={
        "default": AsyncIOExecutor(),
    },
    job_defaults={
        "misfire_grace_time": 300,   # 5 минут — запускаем пропущенные задачи
        "coalesce":           True,  # Несколько пропущенных → один запуск
    },
    timezone="Europe/Moscow",
)


# ── Задача пайплайна ──────────────────────────────────────────────────────────

async def _run_pipeline_job() -> None:
    """
    Точка входа для APScheduler — создаёт прогон и запускает пайплайн.

    Оборачивает pipeline.run_pipeline() в управление run_id и
    защищает планировщик от необработанных исключений.
    """
    # Ленивый импорт во избежание циклических зависимостей при старте
    from core.pipeline import create_pipeline_run, run_pipeline

    try:
        run_id = await create_pipeline_run()
        logger.info(f"[scheduler] Запуск запланированного прогона #{run_id}")
        await run_pipeline(run_id)
    except Exception as exc:
        logger.error(f"[scheduler] Ошибка запланированного прогона: {exc}", exc_info=True)


# ── Управление расписанием ────────────────────────────────────────────────────

async def reload_schedule() -> None:
    """
    Перезагружает расписание из таблицы schedule_slots.

    Вызывается при старте и из admin-панели после изменения слотов.
    Удаляет все текущие pipeline-задачи и создаёт новые из активных слотов.
    """
    # Удаляем все текущие pipeline-задачи
    removed = 0
    for job in scheduler.get_jobs():
        if job.id.startswith("pipeline_slot_"):
            job.remove()
            removed += 1

    if removed:
        logger.info(f"[scheduler] Удалено старых задач: {removed}")

    # Загружаем активные слоты из БД
    async with async_session_factory() as session:
        rows = (await session.execute(
            text(
                "SELECT id, hour, minute, days_of_week "
                "FROM schedule_slots WHERE is_active = 1"
            )
        )).fetchall()

    if not rows:
        logger.warning("[scheduler] Нет активных слотов расписания в БД")
        return

    # Регистрируем cron-задачи для каждого слота
    for slot_id, hour, minute, days in rows:
        scheduler.add_job(
            func=_run_pipeline_job,
            trigger="cron",
            hour=hour,
            minute=minute,
            day_of_week=days,
            id=f"pipeline_slot_{slot_id}",
            replace_existing=True,
            name=f"Pipeline {hour:02d}:{minute:02d} MSK",
        )
        logger.info(
            f"[scheduler] Добавлена задача: slot_{slot_id} "
            f"в {hour:02d}:{minute:02d} ({days})"
        )

    logger.info(f"[scheduler] Расписание загружено: {len(rows)} активных слотов")


async def start_scheduler() -> None:
    """
    Запускает планировщик и загружает расписание из БД.

    Вызывается из main.py при старте приложения.
    """
    await reload_schedule()
    scheduler.start()
    logger.info("[scheduler] APScheduler запущен")

    # Выводим ближайшие задачи для проверки
    jobs = scheduler.get_jobs()
    for job in jobs:
        next_run = job.next_run_time
        if next_run:
            logger.info(
                f"[scheduler] Следующий запуск: {job.id} → "
                f"{next_run.strftime('%Y-%m-%d %H:%M %Z')}"
            )
