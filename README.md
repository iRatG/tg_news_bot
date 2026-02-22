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
[1] Researcher   — парсит RSS, скоринг + tier/brand/diversity, URL-дедупликация
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

## Алгоритм отбора новостей: контентное разнообразие

### Проблема

OpenAI производит больше новостей, чем любой другой AI-игрок. Даже при идеальном скоринге по ключевым словам все 5 кандидатов-финалистов оказываются про ChatGPT — просто потому что их больше в RSS-лентах. Канал превращается в ChatGPT-репортаж вместо панорамы AI-индустрии.

### Решение: три уровня фильтрации без LLM

Весь алгоритм реализован в `agents/researcher.py` через детерминированные правила и математику. **Ни одного AI-вызова** — бесплатно, мгновенно, предсказуемо.

```
Базовый score (ключевые слова + бонус свежести)
       ×
Tier multiplier   (важность контента)
       ×
Diversity multiplier  (недоохваченность бренда)
       =
Adjusted score → сортировка → soft cap → финальные кандидаты
```

---

### Уровень 1 — Tier: важность контента

Каждая статья классифицируется по уровню научной/технической ценности:

| Tier | Множитель | Критерии (в заголовке или тексте) |
|---|---|---|
| `breakthrough` | **×2.0** | `arxiv`, `paper`, `research`, `benchmark`, `study`, `survey`, `architecture`, `weights`, `dataset`, `outperforms`, `исследование`, `разбор`, `бенчмарк` и др. |
| `news` | ×1.0 | Всё остальное — выход модели, обновление сервиса, интеграция |
| `noise` | ×0.5 | `funding`, `investment`, `partnership`, `layoffs`, `acquisition`, `series a/b`, `инвестиции`, `партнерство` и др. |

**Заголовок весит вдвое больше содержимого.** Итого:

```python
t1_score = sum(2 if kw in title else 1 for kw in TIER1_KEYWORDS if kw in text)
# t1_score >= 2  → "breakthrough"
# noise_score >= 2 and t1 == 0  → "noise"
# иначе  → "news"
```

Это решает ключевую задачу: прорывная научная статья от любого бренда получает ×2 приоритет и **никогда не будет вытеснена** маркетинговой новостью с высоким объёмом.

---

### Уровень 2 — Diversity: балансировка по брендам

Перед скорингом читается история публикаций из `published_posts` за последние 7 дней:

```python
brand_ratio = posts_by_brand_last_7d / total_posts_last_7d

diversity_mult = 1.0 + (1.0 - brand_ratio)
```

| Ситуация | brand_ratio | diversity_mult |
|---|---|---|
| Бренд ни разу не публиковался | 0% | **2.0** (максимальный буст) |
| Бренд занимает 25% публикаций | 25% | 1.75 |
| Бренд занимает 50% публикаций | 50% | 1.50 |
| Бренд занимает 100% публикаций | 100% | 1.0 (нет штрафа) |

**Важно:** доминирующий бренд не штрафуется — только недоохваченные получают буст. Если OpenAI выпустил прорывную архитектуру (tier=breakthrough, ×2.0), она попадёт в подборку даже если OpenAI занимает 100% истории публикаций.

Поддерживаемые бренды: `openai`, `anthropic`, `google`, `deepseek`, `meta`, `perplexity`, `mistral`, `xai`, `other`.

---

### Уровень 3 — Soft cap: ограничение в финальной выборке

После сортировки по `adjusted_score` применяется soft cap при формировании топ-5:

```
breakthrough  → всегда включается, без ограничений по бренду
news / noise  → не более BRAND_CAP = 2 статей одного бренда
```

```python
for candidate in sorted_by_adjusted_score:
    if candidate.tier == "breakthrough":
        result.append(candidate)          # всегда
    elif brand_count[candidate.brand] < BRAND_CAP:
        result.append(candidate)          # soft cap
        brand_count[candidate.brand] += 1
    if len(result) >= MAX_RESULTS:
        break
```

---

### Пример из логов

```
[researcher] История брендов за 7 дней: {'openai': 12, 'anthropic': 3, 'google': 2, 'other': 3}
[researcher] Готово: 5/47 кандидатов | пул=200 | [deepseek(b) meta(b) other(b) anthropic(n) google(n)] | 17319мс

  [base= 35 adj= 70.0 tier=B brand=deepseek    ] Simon Willison: Architectural Choices in China's Open-Source AI
  [base= 28 adj= 56.0 tier=B brand=meta        ] LLaMA 3.1: Architecture and Training Details
  [base= 25 adj= 43.8 tier=B brand=other        ] MLflow 2.20: New Evaluation Framework
  [base= 30 adj= 37.5 tier=N brand=anthropic    ] Claude 3.7 Sonnet Update
  [base= 22 adj= 28.6 tier=N brand=google       ] Gemini for Workspace Announcement
```

`b` = breakthrough (tier), `n` = news. Несмотря на то что OpenAI доминирует в истории (12/20 постов), ни одного OpenAI-поста нет: у них нет breakthrough-статей в пуле, а Tier 2/3 вытеснены конкурентами с diversity_mult=2.0.

---

### Почему не LLM для классификации?

| Подход | Стоимость (200 кандидатов) | Latency | Детерминизм |
|---|---|---|---|
| Keyword rules (текущий) | **$0** | ~0мс | **100%** |
| Perplexity sonar на классификацию | ~$0.04/прогон | +200с | ~85% |
| GPT-4o-mini на классификацию | ~$0.02/прогон | +60с | ~90% |

Правила покрывают 90%+ случаев в AI-новостях. LLM добавляет гибкость за счёт стоимости и непредсказуемости — невыгодный обмен для фильтрации на входе пайплайна.

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
│   ├── researcher.py    # RSS-парсинг, tier/brand/diversity scoring, soft cap
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

### Perplexity sonar — артефакты в output
- Генерирует цитаты `[1][2][3]` при веб-поиске → срезаются `re.sub(r'\[\d+\]', '', text)` в writer и formatter
- Генерирует `<br>` теги → заменяются на `\n` в `_validate_html()`
- Генерирует неподдерживаемые Telegram теги (`<p>`, `<div>`, `<span>` и др.) → удаляются регулярным выражением
- `search_context_size: "low"` в Writer/Formatter снижает количество цитат и стоимость запроса

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
- OpenAI, Anthropic — заблокированы (403)
- DeepSeek — блокируется внутри Docker-контейнера (Connection error; с хоста может работать, но в контейнере — нет)
- Perplexity — работает глобально, единственный надёжный AI API с RU VPS
- Семантическая дедупликация (OpenAI embeddings) отключена, используется только URL-дедупликация

### RSS-источники
Из 10 источников стабильно работают ~5–6. Проблемные:
- Google DeepMind, Anthropic — malformed XML
- TLDR AI, ArXiv, The Batch — SSL timeout

`socket.setdefaulttimeout(10)` обязателен перед каждым `feedparser.parse()`.

---

## Выбор модели и оптимизация

### A/B тест: sonar-pro vs sonar (2026-02-22)

Перед выбором модели был проведён объективный тест на реальных данных — 4 статьи через Writer и 6 постов через Formatter. Тест запускается скриптом `scripts/test_quality.py` без публикации в Telegram.

**Метрики качества:**

| Метрика | Что проверяет |
|---|---|
| Длина в диапазоне | Попадает ли в цель: single 400–600 симв., longread 800–1200, digest до 3800 |
| Есть URL | Есть ли ссылка на источник (без неё пост нарушает формат) |
| Есть стартовый эмодзи | Визуальный маркер в ленте Telegram |
| Нет цитат `[1][2][3]` | Perplexity добавляет их из поиска — в посте неуместны |
| Нет плохих HTML-тегов | `<p>`, `<div>`, `<br>` вызывают ошибку Telegram Bot API |

**Результаты (Writer, 4 статьи):**

| Метрика | sonar-pro | sonar |
|---|---|---|
| В целевом диапазоне | 0/4 | **2/4** |
| Есть URL | 2/4 | **4/4** |
| Есть эмодзи | 4/4 | 4/4 |
| Нет цитат | 4/4 | 4/4 |
| Нет плохих тегов | 4/4 | 4/4 |
| Средний latency | 4.1с | **3.4с** |
| Стоимость теста | $0.0162 | **$0.0020** |

**Результаты (Formatter, 6 постов):**

| Метрика | sonar-pro | sonar |
|---|---|---|
| Нет цитат / плохих тегов | 6/6 | 6/6 |
| Средний latency | 2.9с | 2.9с |
| Стоимость теста | $0.0346 | **$0.0045** |

**Вывод:** `sonar` не хуже `sonar-pro` ни по одной метрике и быстрее (-17%). Экономия на Writer — 88%, на Formatter — 87%. Текущая конфигурация бота использует `sonar` для всех агентов.

### Конфигурация `search_context_size`

Perplexity поддерживает параметр `web_search_options.search_context_size` (`low` / `medium` / `high`), который управляет объёмом веб-контекста в промпте:

| Агент | search_context_size | Причина |
|---|---|---|
| Writer | `low` | Контент уже есть из RSS; поиск не нужен, только увеличивает стоимость и добавляет цитаты |
| Formatter | `low` | Задача чисто техническая — добавить HTML-теги; веб-поиск бессмысленен |
| Fact-Checker | `high` | Нужно максимум независимых источников для верификации + `recency_filter=week` |

### Запуск теста качества

```bash
# Локально (нужна .env с PERPLEXITY_API_KEY):
python scripts/test_quality.py --n 5 --formatter

# На VPS (внутри Docker):
docker exec newsbot python scripts/test_quality.py --n 5 --formatter
```

Тест читает реальные опубликованные статьи из БД, прогоняет через обе модели и выводит сравнительную таблицу. Telegram не используется.

---

## Стоимость (примерная, без изображений)

| Компонент | Модель | Цена/день |
|---|---|---|
| Writer (3 поста) | `sonar` + context=low | ~$0.0005 |
| Formatter (3 поста) | `sonar` + context=low | ~$0.0003 |
| Fact-Checker (15 проверок) | `sonar` + context=high | ~$0.001 |
| Leonardo AI (если включено, только single) | — | ~$0.06 |
| **Итого без изображений** | | **~$0.002/день (~$0.05/месяц)** |

> До оптимизации (sonar-pro): ~$0.005/день. **Экономия после A/B теста: ~60%.**
