from __future__ import annotations

"""
Агент 4 — Formatter.

Применяет Telegram HTML-разметку к тексту поста и опционально
генерирует иллюстрацию через Leonardo AI.

Алгоритм:
    1. Передаёт текст в Perplexity sonar-pro с инструкцией добавить HTML-теги Telegram.
    2. Проверяет баланс тегов <b> и корректность <a href="...">.
    3. Если image_enabled=true и LEONARDO_API_KEY задан — генерирует картинку:
       a) sonar-pro создаёт image-prompt (до 100 слов)
       b) Leonardo AI API: POST generations → poll → download bytes в память
       c) При любой ошибке Leonardo — пропускаем картинку, не блокируем пайплайн
    4. Проверяет что итоговый текст <= 1024 символа (лимит Telegram caption).

Примечание: DeepSeek geo-blocked на RU VPS. Perplexity sonar-pro доступен глобально.

Стоимость: ~$0.001/день без картинок; ~$0.06/день с Leonardo AI.
"""

import logging
import time
from dataclasses import dataclass
from typing import Optional

import openai
import requests
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from agents.writer import WriterResult
from core.config import get_setting, settings

logger = logging.getLogger(__name__)

# ── Конфигурация ──────────────────────────────────────────────────────────────

FORMATTER_MODEL     = "sonar-pro"
TELEGRAM_MAX_CHARS  = 1024   # Жёсткий лимит caption/message Telegram
LEONARDO_POLL_SEC   = 3      # Интервал опроса статуса генерации
LEONARDO_TIMEOUT_SEC = 30    # Максимальное ожидание Leonardo


# ── Выходная структура ────────────────────────────────────────────────────────

@dataclass
class FormatterResult:
    """Результат форматирования: HTML-текст + опциональные байты изображения."""

    article_id:     int
    formatted_text: str
    image_bytes:    Optional[bytes]   # None если нет картинки или Leonardo упал
    input_tokens:   int
    output_tokens:  int
    latency_ms:     int

    def __repr__(self) -> str:
        img = f"{len(self.image_bytes)} bytes" if self.image_bytes else "no image"
        return (
            f"<FormatterResult article_id={self.article_id} "
            f"chars={len(self.formatted_text)} {img}>"
        )


# ── Retry ─────────────────────────────────────────────────────────────────────

def _retryable(func):
    """3 попытки при RateLimitError / APIStatusError."""
    return retry(
        retry=retry_if_exception_type(
            (openai.RateLimitError, openai.APIStatusError)
        ),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=4, max=30),
        reraise=True,
    )(func)


# ── HTML форматирование ───────────────────────────────────────────────────────

_FORMAT_SYSTEM = (
    "Ты — редактор Telegram-постов. "
    "Добавляй только HTML-теги, не меняй текст."
)

def _format_prompt(post_text: str) -> str:
    return f"""Отформатируй этот пост для Telegram с HTML-разметкой.

Правила:
- Первую строку (заголовок) оберни в <b>заголовок</b>
- Эмодзи оставь как есть
- Строку с источником: 🔗 <a href="URL">Название источника</a>
- Максимум 4 эмодзи в посте
- НЕ добавляй лишние переносы строк
- Верни ТОЛЬКО отформатированный текст, без объяснений

Текст для форматирования:
{post_text}"""


@_retryable
async def _format_html(post_text: str) -> tuple[str, int, int]:
    """
    Применяет Telegram HTML-разметку через Perplexity sonar-pro.

    Returns:
        (formatted_text, input_tokens, output_tokens)
    """
    client = openai.AsyncOpenAI(
        api_key=settings.PERPLEXITY_API_KEY,
        base_url="https://api.perplexity.ai",
    )
    response = await client.chat.completions.create(
        model=FORMATTER_MODEL,
        messages=[
            {"role": "system", "content": _FORMAT_SYSTEM},
            {"role": "user",   "content": _format_prompt(post_text)},
        ],
        temperature=0.1,
        max_tokens=800,
    )
    text    = response.choices[0].message.content.strip()
    in_tok  = getattr(response.usage, "prompt_tokens",     0)
    out_tok = getattr(response.usage, "completion_tokens", 0)
    return text, in_tok, out_tok


def _validate_html(text: str) -> str:
    """
    Проверяет и исправляет базовые HTML-проблемы.

    - Незакрытый <b> → добавляет </b>
    - Текст длиннее TELEGRAM_MAX_CHARS → жёсткая обрезка до последнего пробела
    """
    # Баланс тега <b>
    if text.count("<b>") != text.count("</b>"):
        logger.warning("[formatter] Дисбаланс тегов <b> — исправляю")
        opens  = text.count("<b>")
        closes = text.count("</b>")
        if opens > closes:
            text += "</b>" * (opens - closes)

    # Жёсткий лимит Telegram
    if len(text) > TELEGRAM_MAX_CHARS:
        logger.warning(
            f"[formatter] Текст {len(text)} симв. > {TELEGRAM_MAX_CHARS} — обрезаю"
        )
        text = text[:TELEGRAM_MAX_CHARS]
        # Обрезаем до последнего пробела чтобы не резать слово
        last_space = text.rfind(" ")
        if last_space > TELEGRAM_MAX_CHARS - 50:
            text = text[:last_space]

    return text


# ── Leonardo AI — генерация изображений ──────────────────────────────────────

@_retryable
async def _generate_image_prompt(post_text: str) -> str:
    """Генерирует краткий image-prompt для Leonardo AI через Perplexity sonar-pro."""
    client = openai.AsyncOpenAI(
        api_key=settings.PERPLEXITY_API_KEY,
        base_url="https://api.perplexity.ai",
    )
    response = await client.chat.completions.create(
        model=FORMATTER_MODEL,
        messages=[{
            "role": "user",
            "content": (
                "Создай краткий image-prompt для Leonardo AI на английском языке "
                "(максимум 80 слов). Стиль: futuristic digital art, no text in image. "
                f"Контекст поста:\n{post_text[:200]}"
            ),
        }],
        temperature=0.8,
        max_tokens=150,
    )
    return response.choices[0].message.content.strip()


def _call_leonardo(image_prompt: str) -> Optional[bytes]:
    """
    Синхронный вызов Leonardo AI API: запрос → polling → скачивание байт.

    Синхронный потому что requests проще для polling-loop.
    Изображение НИКОГДА не сохраняется на диск — только bytes в памяти.

    Returns:
        bytes изображения или None при любой ошибке.
    """
    api_key  = settings.LEONARDO_API_KEY
    model_id = settings.LEONARDO_MODEL_ID
    headers  = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type":  "application/json",
    }

    # 1. Запускаем генерацию
    try:
        resp = requests.post(
            "https://cloud.leonardo.ai/api/rest/v1/generations",
            headers=headers,
            json={
                "modelId":       model_id,
                "prompt":        image_prompt,
                "width":         1024,
                "height":        576,
                "num_images":    1,
                "guidance_scale": 7,
            },
            timeout=15,
        )
        resp.raise_for_status()
        generation_id = resp.json()["sdGenerationJob"]["generationId"]
        logger.info(f"[formatter] Leonardo: generation_id={generation_id}")
    except Exception as exc:
        logger.warning(f"[formatter] Leonardo запрос упал: {exc}")
        return None

    # 2. Polling результата
    poll_url = f"https://cloud.leonardo.ai/api/rest/v1/generations/{generation_id}"
    elapsed  = 0
    while elapsed < LEONARDO_TIMEOUT_SEC:
        time.sleep(LEONARDO_POLL_SEC)
        elapsed += LEONARDO_POLL_SEC
        try:
            poll = requests.get(poll_url, headers=headers, timeout=10)
            poll.raise_for_status()
            data   = poll.json().get("generations_by_pk", {})
            status = data.get("status", "")

            if status == "COMPLETE":
                images = data.get("generated_images", [])
                if not images:
                    logger.warning("[formatter] Leonardo: COMPLETE но нет изображений")
                    return None
                image_url = images[0]["url"]
                break
            elif status == "FAILED":
                logger.warning("[formatter] Leonardo: статус FAILED")
                return None
            else:
                logger.debug(f"[formatter] Leonardo: статус={status}, ждём...")
        except Exception as exc:
            logger.warning(f"[formatter] Leonardo polling ошибка: {exc}")
            return None
    else:
        logger.warning(f"[formatter] Leonardo: таймаут {LEONARDO_TIMEOUT_SEC}с")
        return None

    # 3. Скачиваем изображение в память (не на диск)
    try:
        img_resp = requests.get(image_url, timeout=20)
        img_resp.raise_for_status()
        image_bytes = img_resp.content
        logger.info(f"[formatter] Leonardo: изображение {len(image_bytes)} байт")
        return image_bytes
    except Exception as exc:
        logger.warning(f"[formatter] Ошибка скачивания изображения: {exc}")
        return None


# ── Публичный интерфейс ───────────────────────────────────────────────────────

async def format_post(writer_result: WriterResult) -> FormatterResult:
    """
    Форматирует пост: HTML-разметка + опциональная картинка Leonardo AI.

    Сбой Leonardo AI не прерывает пайплайн — пост уходит без картинки.
    Все решения по изображениям управляются через settings (image_enabled).

    Args:
        writer_result: Результат от агента Writer.

    Returns:
        FormatterResult с отформатированным текстом и bytes картинки (или None).
    """
    t0 = time.monotonic()
    logger.info(f"[formatter] Форматирование поста article_id={writer_result.article_id}")

    total_in_tok  = 0
    total_out_tok = 0

    # ── Шаг 1: HTML-форматирование ────────────────────────────────────────────
    try:
        formatted, in_tok, out_tok = await _format_html(writer_result.post_text)
        total_in_tok  += in_tok
        total_out_tok += out_tok
    except Exception as exc:
        logger.error(f"[formatter] Ошибка HTML-форматирования: {exc}")
        # Fallback: возвращаем оригинальный текст без разметки
        formatted = writer_result.post_text
        logger.warning("[formatter] Используем текст без HTML-разметки (fallback)")

    formatted = _validate_html(formatted)

    # ── Шаг 2: Генерация картинки (опционально) ───────────────────────────────
    image_bytes: Optional[bytes] = None

    image_enabled = await get_setting("image_enabled", "false")
    if image_enabled.lower() == "true" and settings.LEONARDO_API_KEY:
        logger.info("[formatter] Генерация изображения через Leonardo AI...")
        try:
            img_prompt = await _generate_image_prompt(writer_result.post_text)
            logger.debug(f"[formatter] Image prompt: {img_prompt[:80]}")

            # Leonardo синхронный (polling) — запускаем напрямую
            image_bytes = _call_leonardo(img_prompt)
        except Exception as exc:
            # Любая ошибка Leonardo не блокирует публикацию
            logger.warning(f"[formatter] Leonardo полностью упал: {exc}")
            image_bytes = None
    else:
        logger.debug("[formatter] Генерация изображений отключена")

    latency = int((time.monotonic() - t0) * 1000)
    logger.info(
        f"[formatter] OK: {len(formatted)} симв. | "
        f"image={'да' if image_bytes else 'нет'} | "
        f"tokens={total_in_tok}+{total_out_tok} | {latency}мс"
    )

    return FormatterResult(
        article_id=writer_result.article_id,
        formatted_text=formatted,
        image_bytes=image_bytes,
        input_tokens=total_in_tok,
        output_tokens=total_out_tok,
        latency_ms=latency,
    )
