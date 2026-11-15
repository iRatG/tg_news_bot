# Правила разработки и деплоя — tg_news_bot

> Документ написан по итогам тестирования (апрель 2026).
> Каждый пункт — реальная ошибка которая уже случилась.

---

## Безопасность

### ❌ Никогда не хардкодить credentials в коде
```python
# НЕЛЬЗЯ
VPS_PASSWORD = "mypassword123"

# ПРАВИЛЬНО
VPS_PASSWORD = os.getenv("VPS_PASSWORD", "")
```
Credentials → только в `.env`. `.env` → в `.gitignore`. Проверяй `git diff --staged` перед каждым коммитом.

---

## Docker

### ❌ Никогда не останавливать все контейнеры разом
```bash
# НЕЛЬЗЯ — на сервере могут жить и другие сервисы (Caddy, будущие проекты)
docker stop $(docker ps -q)

# ПРАВИЛЬНО — только наш бот
docker stop tg_news_bot
```

### ❌ Не публиковать порт бота наружу
Docker вставляет свои правила iptables **в обход ufw**: `-p 8010:8010` открывает админку
в интернет, даже если в ufw порт закрыт. Только `-p 127.0.0.1:8010:8010`; наружу админку
отдаёт Caddy по HTTPS (`deploy/Caddyfile`). Так и делает `deploy.py`.

### ❌ Секреты не должны попадать в образ
Dockerfile делает `COPY . .`, поэтому `.dockerignore` (лежит в git) исключает `.env`,
`key_param.txt`, `data/`. Секреты — только через `--env-file`. CI проверяет, что образ чистый.

### ⚠️ docker restart не перечитывает .env
После изменения `.env` на сервере — `python deploy.py --rebuild` (пересоздаёт контейнер).

---

## Деплой

```bash
python deploy.py            # код HEAD → в работающий контейнер → restart, ~20 сек
python deploy.py --rebuild  # docker build + новый контейнер (Dockerfile, requirements, .env)
python deploy.py --allow-dirty  # выложить и незакоммиченные правки — только для отладки
```

- Выкладывается `git archive HEAD` целиком — список файлов вести не нужно
  (раньше забытый в списке файл тихо не доезжал до прода).
- Деплой ждёт `/health` и падает с exit 1, если бот не поднялся.
- На сервере в `/opt/tg_news_bot/REVISION` — какой коммит сейчас в проде.
- GitHub Actions **не деплоит** — только проверяет код (ruff, импорт модулей, init_db,
  docker build). Секреты бота в GitHub не храним.

---

## LLM: кто за что отвечает

| Задача | Модель | Почему |
|---|---|---|
| Fact-check + поиск фактов | Perplexity sonar | нужен веб-поиск; заодно отдаёт `key_facts` |
| Writer, Formatter, arXiv, дедуп | DeepSeek (`core/llm.py`) | дешевле в разы, веб не нужен |

### ❌ DeepSeek не должен «знать» факты сам
У DeepSeek нет веб-поиска, его знания устаревают. Writer пишет **только** по контенту
статьи + `verification.key_facts`. Новый промпт с фактами — с тем же запретом.

### ⚠️ thinking-режим DeepSeek съедает max_tokens
Вызывать DeepSeek только через `core.llm.deepseek_chat` — там thinking выключен, пустой
ответ = ошибка с ретраем. Лимиты с запасом (русский ~2–3 симв/токен + HTML-теги):
brief 500, analysis 1800, digest/formatter 3000.

---

## Telegram Bot API

### ⚠️ Обрезанный ответ модели ломает HTML
Ответ, обрезанный по max_tokens посреди тега, Telegram отвергает:
```
Can't parse entities: unclosed start tag at byte offset XXXX
```
Поэтому лимиты выше — с запасом, а formatter балансирует теги.

### ⚠️ При жёсткой обрезке текста проверять границу HTML-тега
```python
# Неправильно — может обрезать внутри <b> или <a href="...
text = text[:max_chars]

# Правильно — откат до закрытого тега
last_gt = cut.rfind(">")
last_lt = cut.rfind("<")
if last_lt > last_gt:  # незакрытый тег — откатиться
    cut = cut[:last_lt]
```

---

## Researcher / База данных

### ❌ Не использовать LIMIT без балансировки источников
Если один RSS-источник имеет 500+ статей в БД, `LIMIT 200 ORDER BY fetched_at DESC` вернёт почти только его статьи.

```sql
-- НЕПРАВИЛЬНО
SELECT ... FROM raw_articles LIMIT 200 ORDER BY fetched_at DESC

-- ПРАВИЛЬНО — не более 20 статей от каждого источника
SELECT ... FROM (
    SELECT ..., ROW_NUMBER() OVER (PARTITION BY source_id ORDER BY fetched_at DESC) AS rn
    FROM raw_articles ...
) WHERE rn <= 20
```

### ⚠️ После добавления фильтра — проверять данные в БД
Код фильтрации может быть правильным, но данные в БД могут не проходить порог. Всегда проверять:
```sql
SELECT s.name, COUNT(*) FROM raw_articles ra
JOIN sources s ON ra.source_id = s.id
WHERE ra.status = 'new' GROUP BY s.name ORDER BY COUNT(*) DESC;
```

---

### ❌ «Свежесть» по fetched_at обманывает на пустой БД
Пул кандидатов берётся по `fetched_at` (когда скачали), а у только что скачанной статьи
он = сейчас, даже если в RSS она от 2024 года. На новой БД (переезд 2026-11) в кандидаты
попали посты двухмесячной давности. Поэтому researcher дополнительно отсекает статьи
с RSS-датой старше `RSS_MAX_AGE_DAYS` (7). То же спасает при добавлении нового источника.

---

## Сервер

Старый VPS (2026) **взломан** и выведен из работы: на нём лежал `.env`, токен бота угнали
(злоумышленник поставил свой вебхук). Все ключи перевыпущены. Сервер настраивается
скриптом `deploy/server_setup.sh` — с нуля за ~5 минут, ничего «руками по памяти».

- SSH — **только по ключу**, вход по паролю и `PermitRootLogin yes` отключены.
- ufw: снаружи 22, 80, 443. fail2ban, автообновления безопасности, swap 2 ГБ,
  ротация логов docker (5×20 МБ).
- Админка — HTTPS через Caddy (Let's Encrypt), бот слушает только `127.0.0.1:8010`.
- Бэкап БД ежедневно 03:30 UTC: локально 14 дней + **копия в Telegram** админу
  (если задан `TELEGRAM_ADMIN_CHAT_ID`) — локальная копия не переживёт потерю сервера.

IP прода, адрес админки, пароли — **не в репозитории** (он публичный). Они в локальном
`.env` / `key_param.txt` (оба в `.gitignore`).

### После любой смены токена бота / подозрения на утечку
```bash
curl -s https://api.telegram.org/bot$TOKEN/getWebhookInfo   # url должен быть пустым
```
Бот вебхуки не использует. Непустой `url` = токен у чужих → BotFather → Revoke.

---

## Чеклист перед коммитом и деплоем

- [ ] `git diff --staged` — нет секретов, IP прода, адреса админки
- [ ] `ruff check --select F,E9 .` — чисто (то же проверяет CI)
- [ ] Закоммичено → `python deploy.py` (или `--rebuild`, если менялись Dockerfile / requirements / .env)
- [ ] Деплой не во время слотов публикации (07:00, 12:00, 16:00, 19:00 МСК)
