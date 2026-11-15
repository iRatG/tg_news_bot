"""
Общий клиент DeepSeek — основная модель для генерации текста.

Разделение ролей (с 2026-11-15):
    Perplexity sonar — только там, где нужен веб-поиск (agents/fact_checker.py).
                       Заодно возвращает key_facts из найденных источников.
    DeepSeek         — всё остальное: writer, formatter, arxiv, dedup.
                       Пишет только по контенту статьи + key_facts, сам в веб не ходит.

Thinking-режим отключён явно: иначе скрытые рассуждения съедают max_tokens и
при скромном бюджете content приходит пустым (проверено на проде 2026-07-31).
Пустой ответ считаем ошибкой — пусть лучше упадёт этап, чем уйдёт пустой пост.
"""

import logging
import re

import openai
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from core.config import settings

logger = logging.getLogger(__name__)

DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_MODEL    = "deepseek-v4-flash"
REQUEST_TIMEOUT   = 120  # сек; длинный пост на flash — обычно 10-30 сек


class EmptyLLMResponse(RuntimeError):
    """Модель вернула пустой content."""


def _client() -> openai.AsyncOpenAI:
    return openai.AsyncOpenAI(
        api_key=settings.DEEPSEEK_API_KEY,
        base_url=DEEPSEEK_BASE_URL,
        timeout=REQUEST_TIMEOUT,
    )


@retry(
    retry=retry_if_exception_type(
        (openai.RateLimitError, openai.APIStatusError,
         openai.APIConnectionError, EmptyLLMResponse)
    ),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=4, max=30),
    reraise=True,
)
async def deepseek_chat(
    system_prompt: str,
    user_prompt: str,
    *,
    max_tokens: int,
    temperature: float = 0.3,
) -> tuple[str, int, int]:
    """
    Один вызов DeepSeek chat completion.

    Returns:
        (text, input_tokens, output_tokens)

    Raises:
        RuntimeError: если DEEPSEEK_API_KEY не задан.
        EmptyLLMResponse: если после ретраев content всё ещё пустой.
    """
    if not settings.DEEPSEEK_API_KEY:
        raise RuntimeError("DEEPSEEK_API_KEY не задан")

    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": user_prompt})

    response = await _client().chat.completions.create(
        model=DEEPSEEK_MODEL,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
        extra_body={"thinking": {"type": "disabled"}},
    )

    text = (response.choices[0].message.content or "").strip()
    if not text:
        finish = response.choices[0].finish_reason
        logger.warning(f"[llm] DeepSeek вернул пустой ответ (finish_reason={finish})")
        raise EmptyLLMResponse(f"empty content, finish_reason={finish}")

    # На случай если модель скопирует сноски вида [1] из key_facts
    text = re.sub(r"\[\d+\]", "", text).strip()

    usage   = response.usage
    in_tok  = getattr(usage, "prompt_tokens",     0) if usage else 0
    out_tok = getattr(usage, "completion_tokens", 0) if usage else 0
    return text, in_tok, out_tok
