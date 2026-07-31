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
        max_tokens=1500,
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

# Whitelist разрешённых Telegram HTML-тегов.
# Всё остальное вида <...> является невалидным и вызывает ошибку Telegram Bot API.
_VALID_TG_TAG = re.compile(
    r'^</?(?:b|i|u|s|code|pre|strong|em|del|strike|tg-spoiler)>$'
    r'|^<a(?:\s[^>]*)?>$'
    r'|^</a>$',
    re.IGNORECASE,
)

# Парные Telegram-теги, которые балансируем через стек.
_TG_PAIRED = {
    "b", "i", "u", "s", "code", "pre",
    "strong", "em", "del", "strike", "tg-spoiler", "a",
}

# Одиночный HTML-тег (открывающий или закрывающий), с возможными атрибутами.
_ANY_TAG = re.compile(r'<(/?)([a-zA-Z][a-zA-Z0-9-]*)(?:\s[^>]*)?>')

# Те же валидные Telegram-теги, но для поиска по всему тексту (не anchored ^...$),
# используется чтобы не экранировать реальные теги при экранировании "голых" &/</>.
_VALID_TAG_SCAN = re.compile(
    r'</?(?:b|i|u|s|code|pre|strong|em|del|strike|tg-spoiler)>'
    r'|<a(?:\s[^<>]*)?>'
    r'|</a>',
    re.IGNORECASE,
)

# Уже валидная HTML-сущность — не трогаем повторным экранированием.
_HTML_ENTITY = re.compile(r'&(?:amp|lt|gt|quot|#\d+|#x[0-9a-fA-F]+);', re.IGNORECASE)


def _escape_plain_text(segment: str) -> str:
    """Экранирует '&', '<', '>' в куске текста, не содержащем реальных тегов."""
    segment = _HTML_ENTITY.sub(lambda m: m.group(0).replace('&', '\x00'), segment)
    segment = segment.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    return segment.replace('\x00', '&')


def _escape_stray_chars(text: str) -> str:
    """
    Экранирует одиночные '&', '<', '>', не входящие в распознанный Telegram-тег
    или уже валидную HTML-сущность.

    Без этого шага обычная лексика AI-новостей (R&D, AT&T, Q&A, <100ms,
    accuracy >90%) приводит к ошибке Telegram Bot API «Can't parse entities» —
    catch-all выше удаляет только конструкции вида <тег>, где есть и открывающая,
    и закрывающая скобка в пределах 200 символов; одиночный непарный '<' или '>'
    (как в «p<0.001» — подтверждённый инцидент в arxiv_agent) он не ловит.
    """
    out: list[str] = []
    pos = 0
    for m in _VALID_TAG_SCAN.finditer(text):
        out.append(_escape_plain_text(text[pos:m.start()]))
        out.append(m.group(0))
        pos = m.end()
    out.append(_escape_plain_text(text[pos:]))
    return "".join(out)

# Запас символов под закрывающие теги, дописываемые балансировщиком после обрезки.
# Иначе итоговая длина может превысить лимит Telegram (и Analyst отбросит пост).
_BALANCE_RESERVE = 40

# Структурные маркеры постов (brief/analysis/digest) — перед каждым, кроме
# самого первого в тексте, должен быть перенос строки (пустая строка).
# Perplexity не всегда следует инструкции "каждый блок с новой строки" — иногда
# склеивает все секции в один абзац (подтверждено на живом посте). Telegram не
# схлопывает переносы как HTML, поэтому без этого пост выглядит нечитаемой стеной текста.
_STRUCTURE_MARKERS = ("📌", "🟡", "🔗", "✔️")
# (?<!<b>) и (?<!<strong>) — маркер иногда попадает ВНУТРЬ жирного заголовка
# (<b>📌 Заголовок</b>), в этом случае перенос перед ним не нужен: это начало
# блока, а не склейка с предыдущим текстом.
_MISSING_BREAK_BEFORE_MARKER = re.compile(
    r'(?<!^)(?<!<b>)(?<!<strong>)[ \t]*\n?[ \t]*(?=' + '|'.join(_STRUCTURE_MARKERS) + ')'
)


def _ensure_line_breaks(text: str) -> str:
    """Гарантирует пустую строку перед каждым структурным маркером поста."""
    text = _MISSING_BREAK_BEFORE_MARKER.sub('\n\n', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


# Вложенные одинаковые парные теги: <b>текст<b>ещё</b></b> → <b>текст ещё</b>.
# Writer уже пишет теги сам, Formatter добавляет их вторым LLM-проходом поверх —
# иногда оборачивает уже жирный заголовок ещё раз (подтверждено на живом посте).
# Оба тега по отдельности валидны и сбалансированы, поэтому _balance_tags их
# не трогает — нужен отдельный проход.
_NESTED_SAME_TAG = re.compile(
    r'<(b|i|u|s|strong|em|del|strike)>([^<]*)<\1>((?:(?!</?\1>).)*)</\1></\1>',
    re.IGNORECASE,
)


def _collapse_nested_tags(text: str) -> str:
    """Схлопывает вложенные одинаковые теги в один (повторяет проход до неподвижной точки)."""
    prev = None
    while prev != text:
        prev = text
        text = _NESTED_SAME_TAG.sub(r'<\1>\2\3</\1>', text)
    return text


def _balance_tags(text: str) -> str:
    """
    Балансирует парные Telegram-теги через стек.

    Telegram Bot API возвращает ошибку при любом непарном теге:
      • «unclosed start tag» / «can't find end tag» — незакрытый <b>/<a>/<i>
      • «unmatched end tag» — лишний закрывающий тег

    Алгоритм:
      • открывающий парный тег → кладём в стек, оставляем в тексте;
      • закрывающий тег top-of-stack → снимаем со стека, оставляем;
      • закрывающий тег глубже в стеке → закрываем промежуточные, затем его;
      • лишний закрывающий тег (нет пары) → удаляем;
      • оставшиеся в стеке открытые теги → закрываем в конце.
    Непарные/неизвестные теги не трогаем (их уже отфильтровал whitelist).
    """
    stack: list[str] = []
    out: list[str] = []
    pos = 0

    for m in _ANY_TAG.finditer(text):
        out.append(text[pos:m.start()])
        pos = m.end()
        is_close = m.group(1) == "/"
        name = m.group(2).lower()

        if name not in _TG_PAIRED:
            out.append(m.group(0))          # не парный — оставляем как есть
            continue

        if not is_close:
            stack.append(name)
            out.append(m.group(0))
            continue

        # Закрывающий парный тег
        if name not in stack:
            continue                        # лишний </...> — выкидываем
        # Закрываем промежуточные незакрытые теги (нарушенная вложенность)
        while stack and stack[-1] != name:
            out.append(f"</{stack.pop()}>")
        if stack:                           # снимаем сам тег
            stack.pop()
            out.append(m.group(0))

    out.append(text[pos:])
    result = "".join(out)

    # Закрываем всё, что осталось открытым, в обратном порядке
    for name in reversed(stack):
        result += f"</{name}>"

    return result


def _validate_html(text: str, max_chars: int = TELEGRAM_MAX_SINGLE) -> str:
    """
    Проверяет и исправляет HTML для Telegram Bot API.

    Telegram поддерживает только: <b>, <i>, <u>, <s>, <a>, <code>, <pre>,
    <strong>, <em>, <del>, <strike>, <tg-spoiler>.

    - <br> → \\n (Telegram не поддерживает <br>)
    - Удаляет известные структурные теги (p, div, span, h1-h6 и др.)
    - Catch-all: удаляет любые <tag> не из whitelist (в т.ч. <20%>, <exploit> и т.д.)
    - Незакрытый <b> → добавляет </b>
    - Текст длиннее max_chars → жёсткая обрезка до последнего пробела
    """
    # Убираем цитаты Perplexity вида [1], [2][3] (второй уровень защиты)
    text = re.sub(r'\[\d+\]', '', text)

    # <br> → перенос строки
    text = re.sub(r'<br\s*/?>', '\n', text, flags=re.IGNORECASE)

    # Гарантируем перенос строки перед каждым структурным маркером (📌/🟡/🔗/✔️),
    # даже если модель слепила все секции в один абзац.
    fixed_breaks = _ensure_line_breaks(text)
    if fixed_breaks != text:
        logger.warning("[formatter] Добавлены переносы строк перед структурными маркерами")
    text = fixed_breaks

    # Удаляем известные неподдерживаемые структурные теги
    cleaned = _UNSUPPORTED_TAGS.sub('', text)
    if cleaned != text:
        logger.warning("[formatter] Удалены неподдерживаемые HTML-теги")
    text = cleaned

    # Catch-all: удаляем любые оставшиеся <...> не из whitelist Telegram.
    # Защита от конструкций вроде <20%>, <500ms>, <exploit>, <уязвимость> и т.д.,
    # которые Telegram пытается распарсить как HTML и возвращает ошибку.
    def _drop_invalid(m: re.Match) -> str:
        return m.group(0) if _VALID_TG_TAG.match(m.group(0)) else ''

    catch_cleaned = re.sub(r'<[^>]{0,200}>', _drop_invalid, text, flags=re.IGNORECASE)
    if catch_cleaned != text:
        logger.warning("[formatter] Catch-all: удалены нераспознанные HTML-конструкции")
    text = catch_cleaned

    # Незавершённый «хвостовой» тег без закрывающего '>' — так Perplexity обрывает
    # ответ на max_tokens прямо внутри <a href="...  Ни catch-all, ни балансировщик
    # его не ловят (обе регулярки требуют '>'), а Telegram падает с «unclosed start tag».
    # Удаляем только конструкцию, похожую на тег ('<' + опц. '/' + буква), чтобы не
    # затронуть текстовые '<' вроде «5 < 10».
    trimmed = re.sub(r'</?[a-zA-Z][^<>]*$', '', text)
    if trimmed != text:
        logger.warning("[formatter] Удалён незавершённый HTML-тег в конце текста")
    text = trimmed

    # Экранируем одиночные &/</>, оставшиеся вне распознанных тегов — иначе
    # обычная лексика («R&D», «<100ms», «accuracy >90%») валит отправку в Telegram.
    escaped = _escape_stray_chars(text)
    if escaped != text:
        logger.warning("[formatter] Экранированы одиночные HTML-спецсимволы (&/</>) вне тегов")
    text = escaped

    # Жёсткий лимит — обрезаем ДО балансировки, оставляя запас под закрывающие теги,
    # чтобы дописанные </b></a> не вытолкнули текст за лимит Telegram.
    limit = max_chars - _BALANCE_RESERVE
    if len(text) > limit:
        logger.warning(
            f"[formatter] Текст {len(text)} симв. > {max_chars} — обрезаю"
        )
        cut = text[:limit]
        # Убеждаемся, что не обрываем внутри HTML-тега (нет незакрытого <...)
        last_gt = cut.rfind(">")
        last_lt = cut.rfind("<")
        if last_lt > last_gt:
            # Незакрытый тег — откатываемся до предыдущего закрытого тега
            cut = cut[:last_lt]
        last_space = cut.rfind(" ")
        if last_space > len(cut) - 50:
            text = cut[:last_space]
        else:
            text = cut

    # Балансировка всех парных тегов — последним шагом, уже после обрезки:
    # закрывает незакрытые <b>/<i>/<a> и удаляет лишние закрывающие теги.
    balanced = _balance_tags(text)
    if balanced != text:
        logger.warning("[formatter] Исправлен дисбаланс HTML-тегов")
    text = balanced

    collapsed = _collapse_nested_tags(text)
    if collapsed != text:
        logger.warning("[formatter] Схлопнуты вложенные одинаковые HTML-теги")
    text = collapsed

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
        if post_format in ("analysis", "longread", "digest")
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
        post_format in ("brief", "single")
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
        if post_format in ("analysis", "longread", "digest"):
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
