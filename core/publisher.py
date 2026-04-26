"""
Модуль публикации в Telegram.

Отправляет готовые посты в канал через Bot API (python-telegram-bot).
Картинка передаётся напрямую как bytes — никакой записи на диск.

Также содержит:
    - notify_admin() — отправка алертов на личный аккаунт
    - verify_bot_token() — проверка валидности токена (для healthcheck)
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from telegram import Bot
from telegram.constants import ParseMode
from telegram.request import HTTPXRequest

from agents.formatter import FormatterResult
from core.config import settings

# Telegram API медленно отвечает с RU VPS (~8-9 сек).
# Увеличиваем таймауты чтобы избежать Timed out при публикации.
_TG_REQUEST = HTTPXRequest(
    connect_timeout=35.0,
    read_timeout=60.0,
    write_timeout=40.0,
    pool_timeout=15.0,
)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def bot_session() -> AsyncGenerator[Bot, None]:
    """
    Контекст-менеджер: создаёт Bot, инициализирует один раз и держит соединение.

    Используется для публикации нескольких постов подряд (arXiv пайплайн)
    чтобы избежать повторного SSL handshake + get_me() на каждый пост.

    Retry до 3 раз при ConnectTimeout — api.telegram.org нестабилен с RU VPS.
    """
    bot = Bot(token=settings.TELEGRAM_BOT_TOKEN, request=_TG_REQUEST)
    last_exc: Exception = RuntimeError("bot_session: не удалось инициализировать")
    for attempt in range(3):
        try:
            await bot.initialize()
            last_exc = None  # type: ignore[assignment]
            break
        except Exception as exc:
            last_exc = exc
            logger.warning(
                f"[publisher] bot.initialize() попытка {attempt + 1}/3: {exc}"
            )
            if attempt < 2:
                await asyncio.sleep(8)

    if last_exc is not None:
        raise last_exc

    try:
        yield bot
    finally:
        try:
            await bot.shutdown()
        except Exception:
            pass


async def send_post(bot: Bot, formatter_result: FormatterResult) -> int:
    """
    Публикует пост через уже инициализированный Bot (без get_me()).

    Retry до 3 раз с 10с задержкой — api.telegram.org нестабилен с RU VPS.
    """
    channel = settings.active_channel
    last_exc: Exception = RuntimeError("send_post: не удалось опубликовать")
    for attempt in range(3):
        try:
            if formatter_result.image_bytes is not None:
                msg = await bot.send_photo(
                    chat_id=channel,
                    photo=formatter_result.image_bytes,
                    caption=formatter_result.formatted_text,
                    parse_mode=ParseMode.HTML,
                )
            else:
                msg = await bot.send_message(
                    chat_id=channel,
                    text=formatter_result.formatted_text,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=False,
                )
            logger.info(
                f"[publisher] Опубликовано: msg_id={msg.message_id} channel={channel}"
            )
            return msg.message_id
        except Exception as exc:
            last_exc = exc
            logger.warning(
                f"[publisher] send_post попытка {attempt + 1}/3: {exc}"
            )
            if attempt < 2:
                await asyncio.sleep(10)
    raise last_exc


async def publish_post(formatter_result: FormatterResult) -> int:
    """
    Публикует пост в Telegram-канал.

    Если есть image_bytes — отправляет фото с caption.
    Иначе — текстовое сообщение с превью ссылки.

    Args:
        formatter_result: Результат агента Formatter с текстом и картинкой.

    Returns:
        message_id опубликованного сообщения.

    Raises:
        telegram.error.TelegramError: при ошибке Bot API.
    """
    channel = settings.active_channel
    bot = Bot(token=settings.TELEGRAM_BOT_TOKEN, request=_TG_REQUEST)

    async with bot:
        if formatter_result.image_bytes is not None:
            msg = await bot.send_photo(
                chat_id=channel,
                photo=formatter_result.image_bytes,
                caption=formatter_result.formatted_text,
                parse_mode=ParseMode.HTML,
            )
            logger.info(
                f"[publisher] Фото опубликовано: msg_id={msg.message_id} "
                f"channel={channel}"
            )
        else:
            msg = await bot.send_message(
                chat_id=channel,
                text=formatter_result.formatted_text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=False,
            )
            logger.info(
                f"[publisher] Сообщение опубликовано: msg_id={msg.message_id} "
                f"channel={channel}"
            )

    return msg.message_id


async def notify_admin(message: str) -> None:
    """
    Отправляет уведомление администратору в личный чат.

    Используется для алертов о сбоях пайплайна и нулевых прогонах.
    Никогда не бросает исключение — сбой уведомления не критичен.

    Args:
        message: Текст уведомления (может содержать HTML).
    """
    admin_id = settings.TELEGRAM_ADMIN_CHAT_ID
    if not admin_id:
        logger.debug("[publisher] TELEGRAM_ADMIN_CHAT_ID не задан — уведомление пропущено")
        return

    try:
        bot = Bot(token=settings.TELEGRAM_BOT_TOKEN, request=_TG_REQUEST)
        async with bot:
            await bot.send_message(
                chat_id=admin_id,
                text=message,
                parse_mode=ParseMode.HTML,
            )
        logger.info(f"[publisher] Уведомление отправлено admin_id={admin_id}")
    except Exception as exc:
        logger.error(f"[publisher] Ошибка отправки уведомления: {exc}")


async def verify_bot_token() -> bool:
    """
    Проверяет валидность Telegram Bot Token через getMe.

    Используется в scripts/healthcheck.py перед деплоем.

    Returns:
        True если токен валиден и бот активен.
    """
    try:
        bot = Bot(token=settings.TELEGRAM_BOT_TOKEN, request=_TG_REQUEST)
        async with bot:
            me = await bot.get_me()
            logger.info(f"[publisher] Bot OK: @{me.username} (id={me.id})")
            return me.is_bot
    except Exception as exc:
        logger.error(f"[publisher] Ошибка проверки токена: {exc}")
        return False
