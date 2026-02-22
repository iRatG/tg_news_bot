import logging
import os

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

load_dotenv()


class Settings:
    # Telegram
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHANNEL_ID: str = os.getenv("TELEGRAM_CHANNEL_ID", "@workhardatassp")
    TELEGRAM_ADMIN_CHAT_ID: str = os.getenv("TELEGRAM_ADMIN_CHAT_ID", "")

    # DeepSeek (chat completions + embeddings)
    DEEPSEEK_API_KEY: str = os.getenv("DEEPSEEK_API_KEY", "")

    # Perplexity Sonar (fact-checking)
    PERPLEXITY_API_KEY: str = os.getenv("PERPLEXITY_API_KEY", "")

    # Leonardo AI (image generation, optional)
    LEONARDO_API_KEY: str = os.getenv("LEONARDO_API_KEY", "")
    LEONARDO_MODEL_ID: str = os.getenv(
        "LEONARDO_MODEL_ID", "b24e16ff-06e3-43eb-8d33-4416c2d75876"
    )

    # Database
    DATABASE_URL: str = os.getenv(
        "DATABASE_URL", "sqlite+aiosqlite:///./data/newsbot.db"
    )

    # Admin panel
    ADMIN_USERNAME: str = os.getenv("ADMIN_USERNAME", "admin")
    ADMIN_PASSWORD: str = os.getenv("ADMIN_PASSWORD", "")

    # LangSmith tracing (optional)
    LANGSMITH_API_KEY: str = os.getenv("LANGSMITH_API_KEY", "")
    LANGSMITH_PROJECT: str = os.getenv("LANGSMITH_PROJECT", "tg-newsbot")

    # System
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    ENVIRONMENT: str = os.getenv("ENVIRONMENT", "development")


settings = Settings()


async def get_setting(key: str, default: str = "") -> str:
    """Read a runtime setting from the DB settings table.

    Falls back to `default` if the key is not found or DB is unavailable.
    This allows live config changes via the admin panel without restarts.
    """
    from sqlalchemy import text
    from db.database import async_session_factory

    try:
        async with async_session_factory() as session:
            result = await session.execute(
                text("SELECT value FROM settings WHERE key = :key"),
                {"key": key},
            )
            row = result.fetchone()
            return row[0] if row else default
    except Exception:
        return default


async def set_setting(key: str, value: str) -> None:
    """
    Записывает или обновляет настройку в таблице settings (UPSERT).

    Используется для сохранения ротируемого стиля постов и других
    динамических параметров без перезапуска сервиса.
    """
    from sqlalchemy import text
    from db.database import async_session_factory

    try:
        async with async_session_factory() as session:
            await session.execute(
                text("""
                    INSERT INTO settings (key, value, updated_at)
                    VALUES (:key, :value, datetime('now'))
                    ON CONFLICT(key) DO UPDATE
                    SET value = :value, updated_at = datetime('now')
                """),
                {"key": key, "value": value},
            )
            await session.commit()
    except Exception as exc:
        logger.error(f"[config] Ошибка записи настройки '{key}': {exc}")
