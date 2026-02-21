# AI News Bot для Telegram

Автономный бот, который находит актуальные новости об AI/LLM, верифицирует их через Perplexity,
пишет посты от лица senior data engineer и публикует в Telegram-канал по расписанию.

**Канал:** [@workhardatassp](https://t.me/workhardatassp)

---

## Пайплайн (5 агентов)

```
RSS-источники
     │
     ▼
[1] Researcher   — парсит 10 RSS, скоринг по ключевым словам, дедуп по URL
     │ кандидат
     ▼
[2] Fact-Checker — Perplexity sonar, confidence score, источники верификации
     │ верифицировано
     ▼
[3] Writer       — Perplexity sonar-pro, пост 400-600 символов, стиль «умный коллега»
     │ черновик
     ▼
[4] Formatter    — Perplexity sonar-pro, Telegram HTML-разметка, опционально Leonardo AI
     │ HTML-пост
     ▼
[5] Analyst      — финальная проверка, семантическая дедупликация, публикация в канал
```

Каждый агент пишет статус в таблицу `agent_runs` (latency, tokens, reason).

---

## Стек

| Слой | Технологии |
|---|---|
| AI | Perplexity sonar / sonar-pro (через OpenAI-совместимый API) |
| Telegram | python-telegram-bot 21.5 |
| БД | SQLite + SQLAlchemy 2 async + aiosqlite |
| Web / Admin | FastAPI + sqladmin + Chart.js |
| Планировщик | APScheduler 3 |
| Деплой | Docker + paramiko |

> **Примечание:** OpenAI и Anthropic заблокированы по гео на RU VPS.
> Все AI-вызовы идут через Perplexity API (глобальная доступность).

---

## Быстрый старт

### 1. Клонировать и настроить окружение

```bash
git clone https://github.com/iRatG/tg_news_bot.git
cd tg_news_bot
cp .env.example .env
# Заполнить .env (см. раздел "Переменные окружения")
```

### 2. Инициализировать БД

```bash
pip install -r requirements.txt
python scripts/init_db.py
```

### 3. Запустить локально

```bash
python main.py
```

Admin-панель: http://localhost:8000/admin
Dashboard: http://localhost:8000/dashboard

### 4. Docker

```bash
docker build -t newsbot:latest .
docker run -d --name newsbot \
  -p 8000:8000 \
  -v $(pwd)/data:/app/data \
  --env-file .env \
  newsbot:latest
```

---

## Деплой на VPS

```bash
# Заполнить в .env:
# VPS_HOST=...  VPS_USER=root  VPS_PASS=...  VPS_DEPLOY_PATH=/opt/tg_news_bot

python scripts/deploy_vps.py
```

Скрипт выполняет: git pull → docker build → init_db → docker run → health check.

---

## Переменные окружения

```env
# AI (обязательно)
PERPLEXITY_API_KEY=pplx-...

# Telegram (обязательно)
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHANNEL_ID=@channel_name
TELEGRAM_ADMIN_CHAT_ID=123456789   # Ваш user_id для уведомлений

# База данных
DATABASE_URL=sqlite+aiosqlite:///./data/newsbot.db

# Admin-панель
ADMIN_USERNAME=admin
ADMIN_PASSWORD=...

# Опционально — изображения через Leonardo AI
LEONARDO_API_KEY=
LEONARDO_MODEL_ID=b24e16ff-06e3-43eb-8d33-4416c2d75876

# Опционально — семантическая дедупликация (не работает на RU VPS)
OPENAI_API_KEY=
```

---

## Структура проекта

```
tg_news_bot/
├── agents/
│   ├── researcher.py    # Парсинг RSS, скоринг кандидатов
│   ├── fact_checker.py  # Верификация через Perplexity
│   ├── writer.py        # Написание поста (sonar-pro)
│   ├── formatter.py     # Telegram HTML + Leonardo AI
│   └── analyst.py       # Финальная проверка + публикация
├── core/
│   ├── pipeline.py      # Оркестрация агентов
│   ├── publisher.py     # Telegram Bot API
│   ├── scheduler.py     # APScheduler расписание
│   ├── dedup.py         # Семантическая дедупликация
│   └── config.py        # Настройки (pydantic-settings)
├── db/
│   ├── models.py        # SQLAlchemy ORM модели
│   └── database.py      # async_session_factory
├── web/
│   ├── admin.py         # FastAPI app + sqladmin + auth
│   └── dashboard.py     # Chart.js API endpoints
├── scripts/
│   ├── init_db.py       # Инициализация БД и seed-данных
│   ├── healthcheck.py   # Pre-deploy проверки
│   └── deploy_vps.py    # Автодеплой через paramiko
├── Dockerfile
├── main.py              # Entrypoint: FastAPI + scheduler
└── requirements.txt
```

---

## Admin-панель

| URL | Описание |
|---|---|
| `/admin` | sqladmin: таблицы, настройки, RSS-источники |
| `/dashboard` | Chart.js: воронка агентов, статистика |
| `/api/pipeline/run` | POST — ручной запуск пайплайна |
| `/health` | GET — статус контейнера |

Авторизация: HTTP Basic Auth (ADMIN_USERNAME / ADMIN_PASSWORD).

---

## Расписание

По умолчанию 3 слота в день (MSK): **09:00 / 14:00 / 19:00**.
Расписание меняется через admin-панель без передеплоя.

---

## Стоимость (примерная)

| Компонент | Цена/день |
|---|---|
| Perplexity sonar-pro (writer + formatter) | ~$0.004 |
| Perplexity sonar (fact-checker) | ~$0.001 |
| Leonardo AI (если включено) | ~$0.06 |
| **Итого без изображений** | **~$0.005/день** |
