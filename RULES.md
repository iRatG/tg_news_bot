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
# НЕЛЬЗЯ — убьёт ВСЕ сервисы на VPS (timemirror и др.)
docker stop $(docker ps -q)

# ПРАВИЛЬНО — только наш бот
docker stop tg_news_bot newsbot 2>/dev/null
```

### ❌ Не делать docker build для изменений .py файлов
```bash
# МЕДЛЕННО (2-3 мин) — только если менялся Dockerfile или requirements.txt
docker build ...

# БЫСТРО (15 сек) — для любых .py изменений
docker cp agents/writer.py tg_news_bot:/app/agents/writer.py
docker restart tg_news_bot
```

### ⚠️ docker restart не подхватывает изменения .env
После изменения `.env` на VPS нужен полный цикл:
```bash
docker stop tg_news_bot && docker rm tg_news_bot
docker run -d --name tg_news_bot --restart unless-stopped \
  -v /opt/tg_news_bot/data:/app/data \
  --env-file /opt/tg_news_bot/.env \
  -p 8010:8010 tg_news_bot
```

---

## Деплой

### Стандартный деплой (изменения .py)
```bash
python deploy.py           # docker cp + restart, ~15 сек
python deploy.py --rebuild # пересборка образа, ~3 мин
```

### После добавления нового агента
Сразу добавь файл в `UPLOAD_PATHS` в `deploy.py`. Иначе файл не попадёт на VPS.

### После изменения .env на VPS
Нужен `docker stop/rm/run` — не просто `restart`. Иначе переменные не обновятся.

---

## Telegram Bot API

### ❌ Не превышать max_tokens при генерации HTML-постов
Perplexity может обрезать ответ прямо внутри HTML-тега → Telegram вернёт:
```
Can't parse entities: unclosed start tag at byte offset XXXX
```
Лимиты с запасом:
- brief: max_tokens=300 (цель ~250 симв)
- analysis: max_tokens=1400 (цель ~1500 симв)
- formatter: max_tokens=1500 (форматирует уже готовый текст)

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

## Другие сервисы на VPS

На том же сервере работают несколько проектов:

| Контейнер | Порт | Управление |
|---|---|---|
| `timemirror-app-1` | 8000 (internal) | `cd /opt/timemirror && docker compose up -d` |
| `timemirror-db-1` | — | то же |
| `tg_news_bot` | 8010 | `deploy.py` |

**Не трогать чужие контейнеры.** Если случайно остановил — `cd /opt/timemirror && docker compose up -d`.

---

## Чеклист перед деплоем

- [ ] `git diff` — нет credentials в изменённых файлах
- [ ] Новые агенты добавлены в `UPLOAD_PATHS` в `deploy.py`
- [ ] Если менялся `.env` — используешь `docker stop/rm/run`, не просто `restart`
- [ ] Если менялся `Dockerfile`/`requirements.txt` — используешь `deploy.py --rebuild`
- [ ] Иначе — `deploy.py` (быстрый docker cp)
