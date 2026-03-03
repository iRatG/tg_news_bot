# AI News Bot для Telegram

Автономный бот для Telegram-канала: находит актуальные AI/LLM-новости, верифицирует их через Perplexity, пишет посты в разных стилях и публикует по расписанию.

**Канал:** [@workhardatassp](https://t.me/workhardatassp)

---

## Как это работает

Два независимых пайплайна:

**Новостной (5 агентов):**
```
RSS (11 источников)
    → [Researcher]   парсинг, скоринг, дедупликация
    → [Fact-Checker] верификация через Perplexity (confidence score)
    → [Writer]       пишет пост в формате brief или analysis
    → [Formatter]    Telegram HTML, очистка артефактов
    → [Analyst]      финальный контроль + публикация
```

**arXiv (научные статьи):**
```
arXiv API (httpx async)
    → фильтр уже виденных (arxiv_seen_papers в БД)
    → Perplexity sonar: обзор на русском
    → публикация в канал
```

Каждый прогон сохраняется в `pipeline_runs`, каждый агент — в `agent_logs`.

---

## Форматы и стили

Три формата постов:
- **brief** — ✔️ короткий новостной бриф, 150–250 симв. Авто-выбор: контент < 800 симв. и нет ключевых слов.
- **analysis** — 📌 аналитический разбор с секциями 🟡, 1200–1800 симв. Авто-выбор: длинный контент или ключевые слова (research, paper, arxiv, interview…).
- **digest** — утренний сборник брифов, до 3800 симв.

---

## Стек

| Слой | Технологии |
|---|---|
| AI | Perplexity sonar (OpenAI-совместимый API) |
| Telegram | python-telegram-bot 21.5 |
| БД | SQLite + SQLAlchemy 2 async + aiosqlite |
| Web / Admin | FastAPI + sqladmin + Chart.js |
| Планировщик | APScheduler 3 (cron, SQLite jobstore) |
| Деплой | Docker + paramiko (scripts/deploy_vps.py) |

> **Гео-ограничение:** OpenAI, Anthropic, DeepSeek заблокированы с RU VPS.
> Все AI-вызовы — через Perplexity API (работает глобально).

---

## Быстрый старт

```bash
git clone https://github.com/iRatG/tg_news_bot.git
cd tg_news_bot
cp .env.example .env   # заполнить (см. ниже)

pip install -r requirements.txt
python scripts/init_db.py
python main.py
```

- Admin-панель: http://localhost:8010/admin
- Dashboard:    http://localhost:8010/dashboard

### Docker

```bash
docker build -t newsbot:latest .
docker run -d --name newsbot \
  -p 8010:8010 \
  -v $(pwd)/data:/app/data \
  --env-file .env \
  newsbot:latest
```

---

## Деплой на VPS

```bash
# Задать в .env: VPS_HOST / VPS_USER / VPS_PASS / VPS_DEPLOY_PATH
python scripts/deploy_vps.py
```

`git pull → docker build → init_db → migrate → docker run → health check`

---

## Расписание (по умолчанию, МСК)

| Время | Режим |
|---|---|
| 07:00 | Утренний дайджест (все верифицированные новости за ночь) |
| 12:00 | Одиночный пост |
| 16:00 | arXiv (научные статьи) |
| 19:00 | Одиночный пост |
| 00:05 | Snapshot числа подписчиков |

Расписание и настройки меняются через `/admin` → Settings без перезапуска.

---

## API

Авторизация: **HTTP Basic Auth** (`ADMIN_USERNAME` / `ADMIN_PASSWORD`).

```bash
# Одиночный прогон
curl -X POST http://HOST:8000/api/pipeline/run -u "admin:PASSWORD"

# Утренний дайджест
curl -X POST "http://HOST:8000/api/pipeline/run?is_morning=true" -u "admin:PASSWORD"

# arXiv прогон
curl -X POST http://HOST:8000/api/pipeline/run_arxiv -u "admin:PASSWORD"
```

Все вызовы возвращают немедленно: `{"run_id": N, "status": "started"}`.

---

## Переменные окружения

```env
# AI (обязательно)
PERPLEXITY_API_KEY=pplx-...

# Telegram (обязательно)
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHANNEL_ID=@channel_name
TELEGRAM_ADMIN_CHAT_ID=123456789

# База данных
DATABASE_URL=sqlite+aiosqlite:///./data/newsbot.db

# Admin-панель
ADMIN_USERNAME=admin
ADMIN_PASSWORD=...

# VPS деплой
VPS_HOST=
VPS_USER=root
VPS_PASS=
VPS_DEPLOY_PATH=/opt/tg_news_bot

# Опционально
OPENAI_API_KEY=         # семантическая дедупликация (не работает с RU VPS)
LEONARDO_API_KEY=       # картинки к постам
```

---

## Структура проекта

```
tg_news_bot/
├── agents/
│   ├── researcher.py    # RSS-парсинг, tier/brand/diversity scoring
│   ├── fact_checker.py  # верификация через Perplexity sonar
│   ├── writer.py        # 2 формата (brief/analysis) + digest, temperature=0.3
│   ├── formatter.py     # Telegram HTML, очистка, Leonardo AI
│   ├── analyst.py       # дедупликация, публикация
│   └── arxiv_agent.py   # arXiv API + Perplexity суммаризация
├── core/
│   ├── pipeline.py      # оркестрация пайплайнов
│   ├── publisher.py     # Telegram Bot API
│   ├── scheduler.py     # APScheduler cron-задачи
│   ├── dedup.py         # семантическая дедупликация
│   └── config.py        # настройки (pydantic-settings)
├── db/
│   ├── models.py        # ORM-модели (9 таблиц)
│   └── database.py      # async engine + session factory
├── web/
│   ├── admin.py         # FastAPI + sqladmin
│   └── dashboard.py     # Chart.js API + ручной запуск
├── templates/
│   ├── base.html
│   └── dashboard.html
├── scripts/
│   ├── init_db.py       # инициализация БД (idempotent)
│   ├── deploy_vps.py    # автодеплой через SSH
│   └── ...              # миграции
├── Dockerfile
├── main.py
└── requirements.txt
```
