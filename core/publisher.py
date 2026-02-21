"""
Модуль публикации в Telegram.

Отправляет готовые посты в канал через Bot API (python-telegram-bot).
Картинка передаётся напрямую как bytes — никакой записи на диск.

Также содержит:
    - notify_admin() — отправка алертов на личный аккаунт
    - verify_bot_token() — проверка валидности токена (для healthcheck)
"""

import logging

from telegram import Bot
from telegram.constants import ParseMode

from agents.formatter import FormatterResult
from core.config import settings

logger = logging.getLogger(__name__)


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
    channel = settings.TELEGRAM_CHANNEL_ID
    bot = Bot(token=settings.TELEGRAM_BOT_TOKEN)

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
        bot = Bot(token=settings.TELEGRAM_BOT_TOKEN)
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
        bot = Bot(token=settings.TELEGRAM_BOT_TOKEN)
        async with bot:
            me = await bot.get_me()
            logger.info(f"[publisher] Bot OK: @{me.username} (id={me.id})")
            return me.is_bot
    except Exception as exc:
        logger.error(f"[publisher] Ошибка проверки токена: {exc}")
        return False
