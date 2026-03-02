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

    Определяет режим запуска: если текущий час (МСК) совпадает с
    morning_digest_hour — запускает режим дайджеста (is_morning=True).
    """
    # Ленивый импорт во избежание циклических зависимостей при старте
    import pytz
    from datetime import datetime
    from core.config import get_setting
    from core.pipeline import create_pipeline_run, run_pipeline

    try:
        morning_enabled = (await get_setting("morning_digest_enabled", "true")).lower() == "true"
        morning_hour    = int(await get_setting("morning_digest_hour", "7"))
        msk_tz          = pytz.timezone("Europe/Moscow")
        now_msk         = datetime.now(msk_tz)
        is_morning      = morning_enabled and (now_msk.hour == morning_hour)

        run_id   = await create_pipeline_run()
        mode_str = "дайджест" if is_morning else "одиночный"
        logger.info(f"[scheduler] Запуск прогона #{run_id} ({mode_str})")
        await run_pipeline(run_id, is_morning=is_morning)
    except Exception as exc:
        logger.error(f"[scheduler] Ошибка запланированного прогона: {exc}", exc_info=True)


async def _run_channel_stats_snapshot() -> None:
    """
    Ежедневный snapshot числа подписчиков Telegram-канала.

    Сохраняет текущее число подписчиков в таблицу channel_stats_history.
    Использует INSERT OR REPLACE — одна запись на день, обновляется при повторном вызове.
    """
    from datetime import datetime as _dt

    from sqlalchemy import text as _text

    from core.config import settings
    from db.database import async_session_factory

    try:
        from telegram import Bot

        bot = Bot(token=settings.TELEGRAM_BOT_TOKEN)
        async with bot:
            count = await bot.get_chat_member_count(chat_id=settings.TELEGRAM_CHANNEL_ID)

        today = _dt.now().strftime("%Y-%m-%d")
        async with async_session_factory() as session:
            await session.execute(
                _text(
                    "INSERT OR REPLACE INTO channel_stats_history "
                    "(date, subscriber_count, fetched_at) "
                    "VALUES (:date, :count, datetime('now'))"
                ),
                {"date": today, "count": count},
            )
            await session.commit()

        logger.info(f"[scheduler] Snapshot подписчиков: {count} ({today})")
    except Exception as exc:
        logger.error(f"[scheduler] Ошибка snapshot подписчиков: {exc}", exc_info=True)


async def _run_morning_digest_job() -> None:
    """
    Утренний дайджест — отдельный cron-джоб, запускается ежедневно.

    Всегда вызывает run_pipeline(is_morning=True), чтобы пайплайн собрал
    все кандидаты за ночь и написал один дайджест-пост из нескольких новостей.

    Выделен в отдельный джоб (по аналогии с arxiv_daily), чтобы дайджест
    гарантированно запускался в morning_digest_hour — независимо от обычных
    schedule_slots (которые используют is_morning=False).
    """
    from core.pipeline import create_pipeline_run, run_pipeline

    try:
        run_id = await create_pipeline_run()
        logger.info(f"[scheduler] Запуск утреннего дайджеста #{run_id}")
        await run_pipeline(run_id, is_morning=True)
    except Exception as exc:
        logger.error(f"[scheduler] Ошибка утреннего дайджеста: {exc}", exc_info=True)


async def _run_arxiv_job() -> None:
    """
    Точка входа для APScheduler — запускает arXiv пайплайн.

    Публикует научные бумаги с arXiv.org как отдельный тип поста.
    """
    from core.pipeline import create_pipeline_run, run_arxiv_pipeline

    try:
        run_id = await create_pipeline_run()
        logger.info(f"[scheduler] Запуск arXiv прогона #{run_id}")
        await run_arxiv_pipeline(run_id)
    except Exception as exc:
        logger.error(f"[scheduler] Ошибка arXiv прогона: {exc}", exc_info=True)


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

    # ── Утренний дайджест ─────────────────────────────────────────────────────
    from core.config import get_setting

    digest_enabled = (await get_setting("morning_digest_enabled", "true")).lower() == "true"
    digest_hour    = int(await get_setting("morning_digest_hour", "7"))

    for job in scheduler.get_jobs():
        if job.id == "morning_digest_daily":
            job.remove()

    if digest_enabled:
        scheduler.add_job(
            func=_run_morning_digest_job,
            trigger="cron",
            hour=digest_hour,
            minute=0,
            day_of_week="mon-sun",
            id="morning_digest_daily",
            replace_existing=True,
            name=f"Morning digest {digest_hour:02d}:00 MSK",
        )
        logger.info(f"[scheduler] Утренний дайджест: ежедневно в {digest_hour:02d}:00 МСК")
    else:
        logger.info("[scheduler] Утренний дайджест отключён (morning_digest_enabled=false)")

    # ── arXiv задача ──────────────────────────────────────────────────────────
    arxiv_enabled = (await get_setting("arxiv_schedule_enabled", "true")).lower() == "true"
    arxiv_hour    = int(await get_setting("arxiv_schedule_hour", "18"))

    # Удаляем старую arxiv задачу перед регистрацией новой
    for job in scheduler.get_jobs():
        if job.id == "arxiv_daily":
            job.remove()

    if arxiv_enabled:
        scheduler.add_job(
            func=_run_arxiv_job,
            trigger="cron",
            hour=arxiv_hour,
            minute=0,
            day_of_week="mon-sun",
            id="arxiv_daily",
            replace_existing=True,
            name=f"arXiv {arxiv_hour:02d}:00 MSK",
        )
        logger.info(f"[scheduler] arXiv задача: ежедневно в {arxiv_hour:02d}:00 МСК")
    else:
        logger.info("[scheduler] arXiv задача отключена (arxiv_schedule_enabled=false)")

    # ── Snapshot подписчиков канала ────────────────────────────────────────────
    scheduler.add_job(
        func=_run_channel_stats_snapshot,
        trigger="cron",
        hour=0,
        minute=5,
        day_of_week="mon-sun",
        id="channel_stats_daily",
        replace_existing=True,
        name="Channel stats snapshot 00:05 MSK",
    )
    logger.info("[scheduler] Snapshot подписчиков: ежедневно в 00:05 МСК")


async def start_scheduler() -> None:
    """
    Запускает планировщик и загружает расписание из БД.

    Вызывается из main.py при старте приложения.

    Порядок важен: scheduler.start() ПЕРВЫМ — только после этого
    get_jobs() видит персистированные задачи из jobstore, и
    reload_schedule() может корректно удалить устаревшие.
    """
    scheduler.start()
    logger.info("[scheduler] APScheduler запущен")
    await reload_schedule()

    # Выводим ближайшие задачи для проверки
    jobs = scheduler.get_jobs()
    for job in jobs:
        next_run = job.next_run_time
        if next_run:
            logger.info(
                f"[scheduler] Следующий запуск: {job.id} → "
                f"{next_run.strftime('%Y-%m-%d %H:%M %Z')}"
            )
