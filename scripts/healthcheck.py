"""
Скрипт предстартовой проверки здоровья — scripts/healthcheck.py.

Запускается CI/CD перед деплоем на VPS.
При любой критической ошибке завершается с кодом 1 — деплой прерывается.

Проверки:
    1. Наличие .env и критических переменных окружения.
    2. Валидность Telegram Bot Token (getMe).
    3. Доступность SQLite БД (простой SELECT).
    4. Доступность OpenAI API (models.list с таймаутом).
    5. Доступность Perplexity API (ping через OpenAI-совместимый endpoint).
"""

import asyncio
import os
import sys

# Добавляем корень проекта в sys.path чтобы импорты работали при запуске
# скрипта напрямую (python scripts/healthcheck.py) из любой директории.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Windows: перенаправляем stdout на UTF-8 чтобы не падать на Unicode в логах
if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("healthcheck")


# ── Константы ─────────────────────────────────────────────────────────────────

REQUIRED_ENV_VARS = [
    "OPENAI_API_KEY",
    "PERPLEXITY_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHANNEL_ID",
]

OPTIONAL_ENV_VARS = [
    "TELEGRAM_ADMIN_CHAT_ID",
    "LEONARDO_API_KEY",
    "LEONARDO_MODEL_ID",
    "ADMIN_PASSWORD",
]


# ── Проверки ──────────────────────────────────────────────────────────────────

def check_env() -> bool:
    """Проверяет наличие всех обязательных переменных окружения."""
    logger.info("[1/5] Проверка переменных окружения...")

    # Загружаем .env если есть
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        logger.warning("  python-dotenv не установлен, читаем из окружения")

    missing = [v for v in REQUIRED_ENV_VARS if not os.getenv(v)]
    if missing:
        logger.error(f"  FAIL — не найдены переменные: {', '.join(missing)}")
        return False

    for var in OPTIONAL_ENV_VARS:
        val = os.getenv(var)
        if not val:
            logger.warning(f"  WARN — опциональная переменная {var!r} не задана")

    logger.info("  OK — все обязательные переменные заданы")
    return True


async def check_telegram() -> bool:
    """Проверяет валидность Telegram Bot Token через getMe."""
    logger.info("[2/5] Проверка Telegram Bot Token...")
    try:
        from core.publisher import verify_bot_token
        ok = await verify_bot_token()
        if ok:
            logger.info("  OK — Telegram Bot активен")
        else:
            logger.error("  FAIL — Telegram Bot недоступен или токен невалиден")
        return ok
    except Exception as exc:
        logger.error(f"  FAIL — исключение: {exc}")
        return False


async def check_database() -> bool:
    """Проверяет доступность SQLite БД."""
    logger.info("[3/5] Проверка базы данных SQLite...")
    try:
        from sqlalchemy import text
        from db.database import async_session_factory

        async with async_session_factory() as session:
            result = await session.execute(text("SELECT COUNT(*) FROM sources"))
            count  = result.scalar()
        logger.info(f"  OK — БД доступна, источников: {count}")
        return True
    except Exception as exc:
        logger.error(f"  FAIL — ошибка БД: {exc}")
        return False


async def check_openai() -> bool:
    """Проверяет доступность OpenAI API."""
    logger.info("[4/5] Проверка OpenAI API...")
    try:
        import openai
        from core.config import settings

        client  = openai.AsyncOpenAI(api_key=settings.OPENAI_API_KEY, timeout=15)
        models  = await client.models.list()
        names   = [m.id for m in models.data[:3]]
        logger.info(f"  OK — OpenAI доступен, примеры моделей: {names}")
        return True
    except Exception as exc:
        logger.error(f"  FAIL — OpenAI недоступен: {exc}")
        return False


async def check_perplexity() -> bool:
    """Проверяет доступность Perplexity API."""
    logger.info("[5/5] Проверка Perplexity API...")
    try:
        import openai
        from core.config import settings

        client   = openai.AsyncOpenAI(
            api_key=settings.PERPLEXITY_API_KEY,
            base_url="https://api.perplexity.ai",
            timeout=15,
        )
        response = await client.chat.completions.create(
            model="sonar",
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=5,
        )
        if response.choices:
            logger.info("  OK — Perplexity API отвечает")
            return True
        else:
            logger.error("  FAIL — Perplexity вернул пустой ответ")
            return False
    except Exception as exc:
        logger.error(f"  FAIL — Perplexity недоступен: {exc}")
        return False


# ── Точка входа ───────────────────────────────────────────────────────────────

async def run_checks() -> int:
    """
    Запускает все проверки последовательно.

    Returns:
        0 — все проверки прошли успешно.
        1 — хотя бы одна критическая проверка провалилась.
    """
    logger.info("=" * 50)
    logger.info("  NewsBot Healthcheck")
    logger.info("=" * 50)

    results: dict[str, bool] = {}

    # Sync checks first
    results["env"]        = check_env()
    if not results["env"]:
        # Без env нет смысла продолжать — нет ключей
        logger.error("Прерываем — нет обязательных переменных окружения")
        return 1

    # Async checks
    results["telegram"]   = await check_telegram()
    results["database"]   = await check_database()
    results["openai"]     = await check_openai()
    results["perplexity"] = await check_perplexity()

    # Итог
    logger.info("-" * 50)
    all_ok = all(results.values())

    for name, ok in results.items():
        status = "OK  " if ok else "FAIL"
        logger.info(f"  {status}  {name}")

    logger.info("-" * 50)
    if all_ok:
        logger.info("  Healthcheck PASSED — деплой разрешён")
    else:
        failed = [k for k, v in results.items() if not v]
        logger.error(f"  Healthcheck FAILED — провалились: {', '.join(failed)}")

    return 0 if all_ok else 1


if __name__ == "__main__":
    exit_code = asyncio.run(run_checks())
    sys.exit(exit_code)
