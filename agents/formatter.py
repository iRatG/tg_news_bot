from __future__ import annotations

"""
Агент 4 — Formatter.

Применяет Telegram HTML-разметку к тексту поста и опционально
генерирует иллюстрацию через Leonardo AI.

Лимиты по формату:
    single   — 1024 символа (с картинкой, Telegram caption limit)
    longread — 4096 символов (без картинки, Telegram message limit)
    digest   — 4096 символов (без картинки, Telegram message limit)

Картинка генерируется ТОЛЬКО для single-поста при image_enabled=true.
Для longread и digest картинка не генерируется независимо от настроек.

Алгоритм:
    1. Читает post_format из WriterResult для выбора лимита и режима картинки.
    2. Передаёт текст в Perplexity sonar-pro с инструкцией добавить HTML-теги Telegram.
    3. Проверяет баланс тегов <b> и корректность <a href="...">.
    4. Если single + image_enabled=true + LEONARDO_API_KEY задан — генерирует картинку:
       a) sonar-pro создаёт image-prompt (до 100 слов)
       b) Leonardo AI API: POST generations → poll → download bytes в память
       c) При любой ошибке Leonardo — пропускаем картинку, не блокируем пайплайн

Примечание: Perplexity sonar-pro доступен глобально с RU VPS.
DeepSeek НЕ работает внутри Docker-контейнера на VPS (Connection error, 2026-02-22).
Стоимость: ~$0.001/день без картинок; ~$0.06/день с Leonardo AI.
"""

import logging
import re
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

FORMATTER_MODEL      = "sonar"    # sonar идентично sonar-pro по качеству HTML, на 87% дешевле
TELEGRAM_MAX_SINGLE  = 1024   # Лимит caption (с картинкой) и short message
TELEGRAM_MAX_LONG    = 4096   # Лимит обычного сообщения (без картинки)
LEONARDO_POLL_SEC    = 3      # Интервал опроса статуса генерации
LEONARDO_TIMEOUT_SEC = 30     # Максимальное ожидание Leonardo


# ── Выходная структура ────────────────────────────────────────────────────────

@dataclass
class FormatterResult:
    """Результат форматирования: HTML-текст + опциональные байты изображения."""

    article_id:     int
    formatted_text: str
    image_bytes:    Optional[bytes]   # None если нет картинки или Leonardo упал
    post_format:    str               # 'single' | 'longread' | 'digest'
    input_tokens:   int
    output_tokens:  int
    latency_ms:     int

    def __repr__(self) -> str:
        img = f"{len(self.image_bytes)} bytes" if self.image_bytes else "no image"
        return (
            f"<FormatterResult article_id={self.article_id} "
            f"format={self.post_format} chars={len(self.formatted_text)} {img}>"
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
    Применяет Telegram HTML-разметку через Perplexity sonar.

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
        extra_body={
            # Formatter только добавляет HTML-теги, веб-поиск не нужен
            "web_search_options": {"search_context_size": "low"},
        },
    )
    text    = response.choices[0].message.content.strip()
    in_tok  = getattr(response.usage, "prompt_tokens",     0)
    out_tok = getattr(response.usage, "completion_tokens", 0)
    return text, in_tok, out_tok


_UNSUPPORTED_TAGS = re.compile(
    r'</?(?:p|div|span|h[1-6]|ul|ol|li|hr|br|table|tr|td|th|thead|tbody|'
    r'section|article|header|footer|blockquote|figure|figcaption)(?:\s[^>]*)?>',
    re.IGNORECASE,
)


def _validate_html(text: str, max_chars: int = TELEGRAM_MAX_SINGLE) -> str:
    """
    Проверяет и исправляет HTML для Telegram Bot API.

    Telegram поддерживает только: <b>, <i>, <u>, <s>, <a>, <code>, <pre>,
    <strong>, <em>, <del>, <strike>, <tg-spoiler>.

    - <br> → \\n (Telegram не поддерживает <br>)
    - Удаляет прочие неподдерживаемые теги (p, div, span, h1-h6 и др.)
    - Незакрытый <b> → добавляет </b>
    - Текст длиннее max_chars → жёсткая обрезка до последнего пробела
    """
    # Убираем цитаты Perplexity вида [1], [2][3] (второй уровень защиты)
    text = re.sub(r'\[\d+\]', '', text)

    # <br> → перенос строки
    text = re.sub(r'<br\s*/?>', '\n', text, flags=re.IGNORECASE)

    # Удаляем неподдерживаемые теги
    cleaned = _UNSUPPORTED_TAGS.sub('', text)
    if cleaned != text:
        logger.warning("[formatter] Удалены неподдерживаемые HTML-теги")
    text = cleaned

    # Баланс тега <b>
    if text.count("<b>") != text.count("</b>"):
        logger.warning("[formatter] Дисбаланс тегов <b> — исправляю")
        opens  = text.count("<b>")
        closes = text.count("</b>")
        if opens > closes:
            text += "</b>" * (opens - closes)

    # Жёсткий лимит
    if len(text) > max_chars:
        logger.warning(
            f"[formatter] Текст {len(text)} симв. > {max_chars} — обрезаю"
        )
        text = text[:max_chars]
        last_space = text.rfind(" ")
        if last_space > max_chars - 50:
            text = text[:last_space]

    return text


# ── Leonardo AI — генерация изображений ──────────────────────────────────────

@_retryable
async def _generate_image_prompt(post_text: str) -> str:
    """Генерирует краткий image-prompt для Leonardo AI через Perplexity sonar."""
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
                "modelId":        model_id,
                "prompt":         image_prompt,
                "width":          1024,
                "height":         576,
                "num_images":     1,
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

    Лимит символов зависит от формата:
        single   — 1024 (с возможной картинкой)
        longread — 4096 (без картинки)
        digest   — 4096 (без картинки)

    Картинка генерируется ТОЛЬКО для single при image_enabled=true.

    Args:
        writer_result: Результат от агента Writer.

    Returns:
        FormatterResult с отформатированным текстом и bytes картинки (или None).
    """
    t0 = time.monotonic()
    post_format = writer_result.post_format
    logger.info(
        f"[formatter] Форматирование поста article_id={writer_result.article_id} "
        f"format={post_format}"
    )

    total_in_tok  = 0
    total_out_tok = 0

    # Лимит символов по формату
    max_chars = (
        TELEGRAM_MAX_LONG
        if post_format in ("longread", "digest")
        else TELEGRAM_MAX_SINGLE
    )

    # ── Шаг 1: HTML-форматирование ────────────────────────────────────────────
    try:
        formatted, in_tok, out_tok = await _format_html(writer_result.post_text)
        total_in_tok  += in_tok
        total_out_tok += out_tok
    except Exception as exc:
        logger.error(f"[formatter] Ошибка HTML-форматирования: {exc}")
        formatted = writer_result.post_text
        logger.warning("[formatter] Используем текст без HTML-разметки (fallback)")

    formatted = _validate_html(formatted, max_chars=max_chars)

    # ── Шаг 2: Генерация картинки (только для single) ─────────────────────────
    image_bytes: Optional[bytes] = None

    image_enabled = await get_setting("image_enabled", "false")
    can_generate_image = (
        post_format == "single"
        and image_enabled.lower() == "true"
        and bool(settings.LEONARDO_API_KEY)
    )

    if can_generate_image:
        logger.info("[formatter] Генерация изображения через Leonardo AI...")
        try:
            img_prompt  = await _generate_image_prompt(writer_result.post_text)
            logger.debug(f"[formatter] Image prompt: {img_prompt[:80]}")
            image_bytes = _call_leonardo(img_prompt)
        except Exception as exc:
            logger.warning(f"[formatter] Leonardo полностью упал: {exc}")
            image_bytes = None
    else:
        if post_format in ("longread", "digest"):
            logger.debug(f"[formatter] Картинка пропущена (format={post_format})")
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
        post_format=post_format,
        input_tokens=total_in_tok,
        output_tokens=total_out_tok,
        latency_ms=latency,
    )
