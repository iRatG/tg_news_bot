# AI News Bot для Telegram

Автономный бот для Telegram-канала: находит актуальные AI/LLM-новости, проверяет их
веб-поиском, пишет посты на русском и публикует по расписанию.

**Канал:** [@workhardatassp](https://t.me/workhardatassp)

---

## Как это работает

Два независимых пайплайна.

**Новостной (5 агентов):**
```
RSS (10 источников)
    → [Researcher]   парсинг, скоринг, отсев архива (RSS-дата > 7 дней)
    → [Fact-Checker] Perplexity + веб-поиск: подтверждение и key_facts из источников
    → [Writer]       DeepSeek: пост brief / analysis / digest по статье + key_facts
    → [Formatter]    DeepSeek: Telegram HTML; баланс тегов, лимиты Telegram
    → [Analyst]      дедуп против истории (DeepSeek) + публикация
```

**arXiv (научные статьи):**
```
arXiv API → фильтр уже виденных (arxiv_seen_papers) → DeepSeek: обзор на русском → канал
```

Каждый прогон сохраняется в `pipeline_runs`, каждый агент — в `agent_logs`
(токены, задержка, причина отказа).

### Почему две модели

| Задача | Модель | Почему |
|---|---|---|
| Проверка фактов | Perplexity `sonar` | нужен поиск в интернете в реальном времени |
| Текст, разметка, arXiv, дедуп | DeepSeek `deepseek-v4-flash` | в разы дешевле; веб не нужен |

DeepSeek сам в интернет не ходит, а его знания устаревают — поэтому Writer пишет **только**
по контенту статьи и фактам, которые Fact-Checker нашёл в независимых источниках.
Это зафиксировано в промптах.

---

## Форматы

- **brief** — ✔️ короткий бриф, 150–250 симв. Короткий контент без ключевых слов.
- **analysis** — 📌 разбор с секциями 🟡, 1200–1800 симв. Длинный контент или ключевые
  слова (research, paper, arxiv, interview…).
- **digest** — утренний сборник брифов, до 3800 симв.

---

## Стек

| Слой | Технологии |
|---|---|
| LLM | DeepSeek (основная), Perplexity sonar (веб-поиск) — OpenAI-совместимый SDK |
| Telegram | python-telegram-bot 21 |
| БД | SQLite + SQLAlchemy 2 async + aiosqlite |
| Web / Admin | FastAPI + sqladmin + Chart.js, за Caddy (HTTPS) |
| Планировщик | APScheduler 3 (cron) |
| Инфраструктура | Docker, Ubuntu, ufw, fail2ban — `deploy/server_setup.sh` |

---

## Быстрый старт (локально)

```bash
git clone https://github.com/iRatG/tg_news_bot.git
cd tg_news_bot
cp .env.example .env   # заполнить DEEPSEEK_API_KEY, PERPLEXITY_API_KEY, TELEGRAM_*
pip install -r requirements.txt
python scripts/init_db.py
python main.py
```

Админка: http://localhost:8010/admin, дашборд: http://localhost:8010/dashboard

---

## Прод

### Обычный деплой

```bash
git commit ...                # деплоится только закоммиченный HEAD
python deploy.py              # код → в контейнер → restart → ждём /health
python deploy.py --rebuild    # если менялись Dockerfile, requirements.txt или .env на сервере
```

Параметры подключения — в локальном `.env`: `VPS_HOST`, `VPS_USER`, `VPS_KEY_FILE`.
Подробности и правила — в [RULES.md](RULES.md).

GitHub Actions только проверяет код (ruff, импорт модулей, init_db, docker build) и
**ничего не деплоит**: секреты бота хранятся только на сервере.

### Развёртывание с нуля (новый сервер)

Ubuntu 24.04+, root по SSH-ключу.

```bash
# 1. Ключ на сервер
ssh-copy-id -i ~/.ssh/tg_news_bot_de root@<IP>
#    в локальный .env: VPS_HOST=<IP>

# 2. Код (упадёт на "Check .env exists" — так и должно быть, код уже выложен)
python deploy.py --rebuild

# 3. Секреты (без строк VPS_*)
grep -v '^VPS_' .env | ssh root@<IP> 'umask 077; cat > /opt/tg_news_bot/.env'

# 4. Сервер: пакеты, SSH только по ключу, ufw, swap, бэкапы, HTTPS-админка
ssh root@<IP> 'bash /opt/tg_news_bot/deploy/server_setup.sh <IP-через-дефисы>.sslip.io'

# 5. БД
ssh root@<IP> 'cd /opt/tg_news_bot && docker build -q -t tg_news_bot . && \
  docker run --rm -v /opt/tg_news_bot/data:/app/data --env-file .env tg_news_bot \
  sh -c "python scripts/init_db.py && python scripts/update_sources_real.py"'

# 6. Запуск
python deploy.py --rebuild
```

Админка будет на `https://<IP-через-дефисы>.sslip.io` (или на вашем домене).

### Бэкапы

Ежедневно в 03:30 UTC (`deploy/backup.sh`): консистентная копия SQLite → сервер, 14 дней,
**и документом в Telegram** админу, если задан `TELEGRAM_ADMIN_CHAT_ID`.
Восстановление: `gunzip` → положить в `/opt/tg_news_bot/data/newsbot.db` при остановленном
контейнере.

---

## Расписание (по умолчанию, МСК)

| Время | Режим |
|---|---|
| 07:00 | Утренний дайджест |
| 12:00 | Одиночный пост |
| 16:00 | Одиночный пост |
| 19:00 | arXiv |
| 00:05 | Snapshot числа подписчиков |

Слоты и настройки меняются в админке → Settings / Schedule Slots.

---

## API

HTTP Basic Auth (`ADMIN_USERNAME` / `ADMIN_PASSWORD`). Все вызовы возвращают сразу
`{"run_id": N, "status": "started"}`.

```bash
curl -X POST https://HOST/api/pipeline/run -u "admin:PASSWORD"                   # одиночный пост
curl -X POST "https://HOST/api/pipeline/run?is_morning=true" -u "admin:PASSWORD" # дайджест
curl -X POST https://HOST/api/pipeline/run_arxiv -u "admin:PASSWORD"             # arXiv
```

---

## Переменные окружения

См. [.env.example](.env.example). Обязательные: `DEEPSEEK_API_KEY`, `PERPLEXITY_API_KEY`
(без него Fact-Checker отклоняет все статьи), `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHANNEL_ID`,
`ADMIN_PASSWORD`. Желательно `TELEGRAM_ADMIN_CHAT_ID` — уведомления об ошибках и бэкапы.

---

## Структура проекта

```
tg_news_bot/
├── agents/
│   ├── researcher.py    # RSS, скоринг tier/brand/diversity, отсев архива
│   ├── fact_checker.py  # Perplexity + веб-поиск → verified, key_facts
│   ├── writer.py        # DeepSeek: brief / analysis / digest
│   ├── formatter.py     # DeepSeek: Telegram HTML; Leonardo AI (опц.)
│   ├── analyst.py       # контроль качества, дедуп, публикация
│   └── arxiv_agent.py   # arXiv API + DeepSeek-обзор
├── core/
│   ├── llm.py           # единый клиент DeepSeek (thinking off, ретраи)
│   ├── pipeline.py      # оркестрация пайплайнов
│   ├── publisher.py     # Telegram Bot API
│   ├── scheduler.py     # APScheduler
│   ├── dedup.py         # семантический дедуп (DeepSeek)
│   └── config.py        # настройки из .env и БД
├── db/                  # ORM-модели, async engine
├── web/                 # FastAPI + sqladmin, дашборд
├── deploy/
│   ├── server_setup.sh  # настройка сервера с нуля (идемпотентно)
│   ├── backup.sh        # ежедневный бэкап БД (+ Telegram)
│   └── Caddyfile        # HTTPS для админки
├── scripts/             # init_db, миграции, ручные прогоны
├── deploy.py            # деплой HEAD на прод
├── Dockerfile, .dockerignore
└── main.py
```
