# ── Образ: Python 3.11 slim (меньше 200MB) ───────────────────────────────────
FROM python:3.11-slim

# Метаданные
LABEL maintainer="newsbot"
LABEL description="Telegram AI NewsBot — 5-agent pipeline"

# ── Системные зависимости ─────────────────────────────────────────────────────
# ca-certificates — для SSL при работе с API
# curl            — для HEALTHCHECK
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        curl && \
    rm -rf /var/lib/apt/lists/*

# ── Рабочая директория ────────────────────────────────────────────────────────
WORKDIR /app

# ── Python зависимости ────────────────────────────────────────────────────────
# Копируем только requirements.txt первым слоем — Docker кеширует слой
# пока файл не изменился (экономим время на CI).
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# ── Исходный код ──────────────────────────────────────────────────────────────
COPY . .

# ── Директории для данных (монтируются как volume в production) ───────────────
RUN mkdir -p /app/data /app/logs

# ── Переменные окружения контейнера ──────────────────────────────────────────
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATABASE_URL="sqlite+aiosqlite:////app/data/newsbot.db" \
    LOG_DIR="/app/logs"

# ── Порт FastAPI ──────────────────────────────────────────────────────────────
EXPOSE 8000

# ── Healthcheck — HTTP-пинг каждые 30 секунд ─────────────────────────────────
# /health возвращает 200 JSON если бот работает нормально
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# ── Точка входа ───────────────────────────────────────────────────────────────
CMD ["python", "main.py"]
