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
| AI | DeepSeek (chat completions) + Perplexity sonar (fact-check, опционально) |
| Telegram | python-telegram-bot 21.5 |
| БД | SQLite + SQLAlchemy 2 async + aiosqlite |
| Web / Admin | FastAPI + sqladmin + Chart.js |
| Планировщик | APScheduler 3 (cron, SQLite jobstore) |
| Деплой | Docker + SSH (paramiko) |

> **Гео-ограничение:** OpenAI, Anthropic заблокированы с RU VPS.
> DeepSeek и Perplexity работают глобально — используем их.

---

## Быстрый старт (локально)

```bash
git clone https://github.com/iRatG/tg_news_bot.git
cd tg_news_bot
cp .env.example .env   # заполнить обязательные поля
pip install -r requirements.txt
python scripts/init_db.py
python main.py
```

- Admin-панель: http://localhost:8010/admin
- Dashboard:    http://localhost:8010/dashboard

### Docker (локально)

```bash
docker build -t newsbot:latest .
docker run -d --name newsbot \
  -p 8010:8010 \
  -v $(pwd)/data:/app/data \
  --env-file .env \
  newsbot:latest
```

---

## Деплой на VPS (Ubuntu + Docker)

> Проверено на Ubuntu 24.04, Docker 29.x, порт 8010.

### Шаг 1 — Подготовить .env

Скопировать `.env.example` в `.env` и заполнить:
- `DEEPSEEK_API_KEY` — обязательно
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHANNEL_ID`, `TELEGRAM_ADMIN_CHAT_ID` — обязательно
- `ADMIN_PASSWORD` — пароль веб-панели (сгенерировать надёжный)
- `DATABASE_URL=sqlite+aiosqlite:////app/data/newsbot.db` — **4 слеша**, абсолютный путь для Docker

### Шаг 2 — Скопировать код на VPS

```bash
rsync -az --exclude='.git' --exclude='*.db' --exclude='.env' \
  ./ root@YOUR_VPS_IP:/opt/tg_news_bot/
# .env копируем отдельно
scp .env root@YOUR_VPS_IP:/opt/tg_news_bot/.env
```

### Шаг 3 — Инициализировать БД (ОБЯЗАТЕЛЬНО перед первым запуском)

```bash
ssh root@YOUR_VPS_IP
cd /opt/tg_news_bot
mkdir -p data logs
docker build -t newsbot:latest .
docker run --rm \
  -v /opt/tg_news_bot/data:/app/data \
  --env-file .env \
  newsbot:latest python scripts/init_db.py
```

### Шаг 4 — Запустить контейнер

```bash
docker run -d \
  --name newsbot \
  --restart unless-stopped \
  -p 8010:8010 \
  -v /opt/tg_news_bot/data:/app/data \
  -v /opt/tg_news_bot/logs:/app/logs \
  --env-file /opt/tg_news_bot/.env \
  newsbot:latest
```

### Проверка

```bash
docker ps                         # newsbot должен быть Up (healthy)
docker logs newsbot --tail 20     # scheduler загрузил слоты — всё ок
curl http://localhost:8010/health
```

### Обновление кода

```bash
# Скопировать новый код (rsync, как в шаге 2)
docker build -t newsbot:latest /opt/tg_news_bot
docker stop newsbot && docker rm newsbot
# Повторить шаг 4
```

> ⚠️ **Важно:** `init_db.py` идемпотентен — безопасно запускать повторно при обновлении.
> Если добавлены новые таблицы — запусти миграцию из `scripts/migrate_*.py`.

---

## Расписание (по умолчанию, МСК)

| Время | Режим |
|---|---|
| 07:00 | Утренний дайджест (все верифицированные новости за ночь) |
| 09:00 | Одиночный пост |
| 14:00 | Одиночный пост |
| 18:00 | arXiv (научные статьи) |
| 19:00 | Одиночный пост |
| 00:05 | Snapshot числа подписчиков |

Расписание и настройки меняются через `/admin` → Settings без перезапуска.

---

## API

Авторизация: **HTTP Basic Auth** (`ADMIN_USERNAME` / `ADMIN_PASSWORD`).

```bash
# Одиночный прогон
curl -X POST http://HOST:8010/api/pipeline/run -u "admin:PASSWORD"

# Утренний дайджест
curl -X POST "http://HOST:8010/api/pipeline/run?is_morning=true" -u "admin:PASSWORD"

# arXiv прогон
curl -X POST http://HOST:8010/api/pipeline/run_arxiv -u "admin:PASSWORD"
```

Все вызовы возвращают немедленно: `{"run_id": N, "status": "started"}`.

---

## Переменные окружения

```env
# AI (обязательно)
DEEPSEEK_API_KEY=sk-...

# Telegram (обязательно)
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHANNEL_ID=@channel_name
TELEGRAM_ADMIN_CHAT_ID=123456789

# База данных
# Локально: sqlite+aiosqlite:///./data/newsbot.db
# VPS/Docker: sqlite+aiosqlite:////app/data/newsbot.db  (4 слеша!)
DATABASE_URL=sqlite+aiosqlite:///./data/newsbot.db

# Admin-панель
ADMIN_USERNAME=admin
ADMIN_PASSWORD=...

# VPS деплой
VPS_HOST=
VPS_USER=root
VPS_PASSWORD=

# Опционально
PERPLEXITY_API_KEY=      # fact-checking (работает с RU VPS)
LEONARDO_API_KEY=        # картинки к постам
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
│   └── config.py        # настройки
├── db/
│   ├── models.py        # ORM-модели (9 таблиц)
│   └── database.py      # async engine + session factory
├── web/
│   ├── admin.py         # FastAPI + sqladmin
│   └── dashboard.py     # Chart.js API + ручной запуск
├── scripts/
│   ├── init_db.py       # инициализация БД (idempotent, запускать перед первым стартом)
│   └── migrate_*.py     # миграции схемы
├── Dockerfile
├── main.py
└── requirements.txt
```
