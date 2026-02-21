"""
Точка входа приложения — main.py.

Запускает FastAPI (admin-панель + dashboard) и APScheduler
в одном процессе на одном event loop.

Порядок запуска:
    1. Настройка логирования
    2. Запуск APScheduler с расписанием из БД
    3. Запуск uvicorn (FastAPI) — блокирует до Ctrl+C

Один процесс намеренно: 1 vCPU / 1GB RAM на VPS.
FastAPI + Scheduler + Агенты — всё в одном asyncio event loop.
"""

import asyncio
import logging

import uvicorn

from core.logger import setup_logging
from core.scheduler import start_scheduler
from web.admin import app  # FastAPI-приложение

logger = logging.getLogger(__name__)


async def main() -> None:
    """Инициализирует компоненты и запускает сервер."""

    # 1. Логирование
    setup_logging()
    logger.info("=" * 50)
    logger.info("  NewsBot starting up")
    logger.info("=" * 50)

    # 2. Планировщик — запускаем до uvicorn (он не блокирует)
    await start_scheduler()
    logger.info("[main] Scheduler started")

    # 3. uvicorn — блокирующий вызов, держит event loop до SIGTERM
    config = uvicorn.Config(
        app=app,
        host="0.0.0.0",
        port=8000,
        log_level="warning",    # uvicorn тихий — наш logger уже настроен
        access_log=False,       # Снижаем шум в логах
        loop="asyncio",
    )
    server = uvicorn.Server(config)

    logger.info("[main] Starting FastAPI on http://0.0.0.0:8000")
    await server.serve()

    logger.info("[main] Shutdown complete")


if __name__ == "__main__":
    asyncio.run(main())
