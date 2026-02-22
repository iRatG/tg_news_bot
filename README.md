# AI News Bot для Telegram

Автономный бот, который находит актуальные новости об AI/LLM, верифицирует их через Perplexity,
пишет посты в одном из 4 стилей и публикует в Telegram-канал по расписанию.

**Канал:** [@workhardatassp](https://t.me/workhardatassp)

---

## Пайплайн (5 агентов)

```
RSS-источники (10 источников)
     │
     ▼
[1] Researcher   — парсит RSS, скоринг по ключевым словам, URL-дедупликация
     │ топ-5 кандидатов
     ▼
[2] Fact-Checker — Perplexity sonar (веб-поиск), confidence score, источники
     │ верифицировано
     ▼
[3] Writer       — Perplexity sonar-pro, определяет формат и стиль поста
     │ черновик
     ▼
[4] Formatter    — Perplexity sonar-pro, Telegram HTML-разметка, опц. Leonardo AI
     │ HTML-пост
     ▼
[5] Analyst      — качество, семантическая дедупликация, публикация в канал
```

Каждый агент пишет статус в `agent_logs` (latency, tokens, reason).
Каждый прогон — запись в `pipeline_runs`.

---

## Форматы постов

Бот публикует в одном из трёх форматов, определяемых автоматически:

### `single` — одиночная новость (400–600 символов)
```
🤖 Заголовок — суть в одну строку

Что произошло — 2-3 предложения фактически.
Почему важно — что изменится в работе AI-разработчика.

🔗 Источник (URL)
```
Формат по умолчанию для большинства новостей.

### `longread` — структурированный разбор (800–1200 символов)
```
📌 Заголовок

🟡 Что сделали
Подробный технический разбор...

🟡 Как это работает
Архитектура, цифры, детали...

🟡 Что это значит
Практические выводы...

источник.com (URL)
```
Активируется автоматически при ключевых словах в заголовке:
`research`, `paper`, `benchmark`, `survey`, `deep dive`, `arxiv`,
`исследование`, `разбор`, `анализ` и др., или при объёме контента > 2000 символов.

### `digest` — утренний дайджест (до 3800 символов)
```
✔️ Заголовок первой новости
Краткое описание 3-4 предложения.
источник.com (URL)

✔️ Заголовок второй новости
Краткое описание 3-4 предложения.
источник.com (URL)

...
```
Запускается каждое утро в 7:00 МСК (настраивается). Собирает все
верифицированные новости в один пост. При 1-2 новостях — более подробно (4-6 предложений).

---

## Стили написания (ротация)

Для `single` и `longread` стиль автоматически ротируется по кругу:

| Стиль | Описание |
|---|---|
| `curator` | Нейтральный, информативный, без оценок. Редактор канала. |
| `tech_analyst` | Технический разбор изнутри: архитектура, числа, детали. Инженер объясняет инженеру. |
| `practitioner` | Что применимо прямо сейчас. Прямые рекомендации практика. |
| `skeptic` | Ограничения, что преувеличено, что умолчали. Конструктивный критик. |

Текущий стиль виден и редактируется в `/admin` → Settings → `post_style_current`.
Дайджест всегда использует стиль `curator`.

---

## Стек

| Слой | Технологии |
|---|---|
| AI | Perplexity sonar / sonar-pro (OpenAI-совместимый API) |
| Telegram | python-telegram-bot 21.5 |
| БД | SQLite + SQLAlchemy 2 async + aiosqlite |
| Web / Admin | FastAPI + sqladmin + Chart.js |
| Планировщик | APScheduler 3 (cron, SQLite jobstore) |
| Деплой | Docker + paramiko (scripts/deploy_vps.py) |

> **Гео-ограничения:** OpenAI, Anthropic, DeepSeek заблокированы с RU VPS.
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

- Admin-панель: http://localhost:8000/admin
- Dashboard:    http://localhost:8000/dashboard

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
# Задать в .env:
# VPS_HOST=...  VPS_USER=root  VPS_PASS=...  VPS_DEPLOY_PATH=/opt/tg_news_bot

python scripts/deploy_vps.py
```

Скрипт: `git pull → docker build → init_db → docker stop/rm → docker run → health check`.

---

## API и команды

Авторизация всех API-эндпоинтов: **HTTP Basic Auth** (`ADMIN_USERNAME` / `ADMIN_PASSWORD`).

### Запуск прогонов вручную

```bash
# Одиночный прогон (обычная новость)
curl -X POST http://HOST:8000/api/pipeline/run \
  -u "admin:PASSWORD"

# Утренний дайджест (все верифицированные новости в один пост)
curl -X POST "http://HOST:8000/api/pipeline/run?is_morning=true" \
  -u "admin:PASSWORD"
```

Оба вызова возвращают немедленно: `{"run_id": N, "status": "started", "mode": "single"|"digest"}`.

### Web-интерфейс

| URL | Описание |
|---|---|
| `GET  /health` | Статус контейнера |
| `GET  /admin` | sqladmin: все таблицы, настройки, RSS-источники |
| `GET  /dashboard` | Chart.js: воронка агентов, топ источников, токены |
| `POST /api/pipeline/run` | Ручной запуск (одиночный) |
| `POST /api/pipeline/run?is_morning=true` | Ручной запуск (дайджест) |
| `GET  /api/dashboard/funnel` | Статистика по агентам (JSON) |
| `GET  /api/dashboard/sources` | Топ RSS-источников (JSON) |
| `GET  /api/dashboard/timeline` | Публикации по дням (JSON) |
| `GET  /api/dashboard/costs` | Расход токенов по агентам (JSON) |
| `GET  /api/dashboard/recent_posts` | Последние 10 публикаций (JSON) |

### Тестирование форматов

```bash
# Проверить формат одиночного поста (стиль берётся из post_style_current)
curl -X POST http://HOST:8000/api/pipeline/run -u "admin:PASSWORD"

# Проверить утренний дайджест
curl -X POST "http://HOST:8000/api/pipeline/run?is_morning=true" -u "admin:PASSWORD"

# Сменить текущий стиль на skeptic через admin
# /admin → Settings → post_style_current → "skeptic"
```

---

## Расписание

По умолчанию 3 слота в день (МСК): **09:00 / 14:00 / 19:00**.
Утренний дайджест: **07:00** (управляется настройкой `morning_digest_hour`).

Расписание и все настройки меняются через `/admin` → Settings без перезапуска.

### Ключевые настройки (`/admin` → Settings)

| Ключ | Значение по умолчанию | Описание |
|---|---|---|
| `post_style_current` | `curator` | Текущий стиль: curator / tech_analyst / practitioner / skeptic |
| `morning_digest_enabled` | `true` | Включить утренний дайджест |
| `morning_digest_hour` | `7` | Час дайджеста (МСК, 0–23) |
| `posts_per_run` | `1` | Постов за одиночный прогон |
| `max_candidates` | `5` | Кандидатов от Researcher за прогон |
| `min_fact_check_conf` | `0.65` | Минимальный confidence для прохождения Fact-Checker |
| `dedup_threshold` | `0.80` | Порог cosine similarity для семантической дедупликации |
| `dedup_lookback_days` | `30` | Окно дедупликации (дней) |
| `image_enabled` | `false` | Картинки через Leonardo AI (только single) |

---

## Переменные окружения

```env
# AI (обязательно)
PERPLEXITY_API_KEY=pplx-...

# Telegram (обязательно)
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHANNEL_ID=@channel_name
TELEGRAM_ADMIN_CHAT_ID=123456789   # user_id для алертов

# База данных
DATABASE_URL=sqlite+aiosqlite:///./data/newsbot.db

# Admin-панель
ADMIN_USERNAME=admin
ADMIN_PASSWORD=...

# Опционально — изображения через Leonardo AI (только для single-постов)
LEONARDO_API_KEY=
LEONARDO_MODEL_ID=b24e16ff-06e3-43eb-8d33-4416c2d75876

# Опционально — семантическая дедупликация (не работает на RU VPS из-за гео-блокировки)
OPENAI_API_KEY=

# VPS деплой
VPS_HOST=
VPS_USER=root
VPS_PASS=
VPS_DEPLOY_PATH=/opt/tg_news_bot
```

---

## Структура проекта

```
tg_news_bot/
├── agents/
│   ├── researcher.py    # RSS-парсинг, скоринг по ключевым словам
│   ├── fact_checker.py  # Верификация через Perplexity sonar
│   ├── writer.py        # 4 стиля, 3 формата, write_post() + write_digest()
│   ├── formatter.py     # Telegram HTML, очистка артефактов, Leonardo AI
│   └── analyst.py       # Дедупликация, публикация в канал
├── core/
│   ├── pipeline.py      # Оркестрация: single-прогон + digest-прогон
│   ├── publisher.py     # Telegram Bot API (send_message / send_photo)
│   ├── scheduler.py     # APScheduler + определение режима дайджеста
│   ├── dedup.py         # Семантическая дедупликация (embeddings)
│   └── config.py        # Settings (pydantic-settings) + get/set_setting()
├── db/
│   ├── models.py        # SQLAlchemy ORM (7 таблиц)
│   └── database.py      # async_session_factory + engine
├── web/
│   ├── admin.py         # FastAPI app + sqladmin + HTTP Basic Auth
│   └── dashboard.py     # Chart.js API endpoints
├── templates/
│   ├── base.html        # Dark-theme layout
│   └── dashboard.html   # Chart.js дашборд
├── scripts/
│   ├── init_db.py       # Инициализация БД и seed-данных
│   ├── healthcheck.py   # Pre-deploy проверки (токен, DB)
│   └── deploy_vps.py    # Автодеплой через paramiko SSH
├── Dockerfile
├── main.py              # Entrypoint: FastAPI + APScheduler
└── requirements.txt
```

---

## Известные особенности и решения

### Perplexity sonar-pro — артефакты в output
- Генерирует цитаты `[1][2][3]` при веб-поиске → срезаются `re.sub(r'\[\d+\]', '', text)` в writer и formatter
- Генерирует `<br>` теги → заменяются на `\n` в `_validate_html()`
- Генерирует неподдерживаемые Telegram теги (`<p>`, `<div>`, `<span>` и др.) → удаляются регулярным выражением

### Telegram Bot API HTML-режим
Поддерживаются только теги: `<b>`, `<i>`, `<u>`, `<s>`, `<a href>`, `<code>`, `<pre>`, `<tg-spoiler>`.
Все остальные теги вызывают ошибку `Can't parse entities: unsupported start tag`.

### Лимиты символов
| Формат | Лимит | Причина |
|---|---|---|
| `single` без картинки | 4096 | Telegram message limit |
| `single` с картинкой | 1024 | Telegram caption limit |
| `longread` | 4096 | Telegram message limit |
| `digest` | 4096 | Telegram message limit |

### Гео-блокировки на RU VPS
- OpenAI, Anthropic, DeepSeek — заблокированы (403 / Connection error)
- Perplexity — работает глобально
- Семантическая дедупликация (OpenAI embeddings) отключена, используется только URL-дедупликация

### RSS-источники
Из 10 источников стабильно работают ~5–6. Проблемные:
- Google DeepMind, Anthropic — malformed XML
- TLDR AI, ArXiv, The Batch — SSL timeout

`socket.setdefaulttimeout(10)` обязателен перед каждым `feedparser.parse()`.

---

## Стоимость (примерная, без изображений)

| Компонент | Цена/день |
|---|---|
| Perplexity sonar-pro (writer + formatter) | ~$0.004 |
| Perplexity sonar (fact-checker) | ~$0.001 |
| Leonardo AI (если включено, только single) | ~$0.06 |
| **Итого без изображений** | **~$0.005/день (~$0.15/месяц)** |
