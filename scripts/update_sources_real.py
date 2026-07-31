"""
Миграция RSS-источников: заменяет Google Alerts на настоящие фиды производителей.

Контекст: scripts/update_sources_rss.py (2026-02-23) заменил 5 прямых RSS на
Google Alerts, потому что они падали по SSL-timeout с тогдашнего RU VPS.
Сервер с тех пор перенесён именно для решения проблем с доступом к внешним API.
Прямые URL перепроверены вживую с текущего VPS (httpx.get изнутри контейнера,
2026-07-31) — работают. Заменяем Google Alerts обратно на производителей.

Итог: 6 источников были Google Alerts под именами компаний (агрегация ЛЮБЫХ
упоминаний слова в интернете, не блог самой компании) → после миграции 10
источников, все настоящие (первичный контент или явно помеченное community-
зеркало), без замены остаются только DeepSeek и The Batch — у них подтверждённо
нет публичного RSS вообще (см. план).

Безопасно запускать многократно (идемпотентно), по образцу update_sources_rss.py.

Usage: python scripts/update_sources_real.py
"""

import asyncio
import sys
from pathlib import Path

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

from sqlalchemy import text
from db.database import async_session_factory

# Маппинг: имя источника → настоящий URL (проверен вживую с прод-VPS 2026-07-31)
URL_UPDATES = {
    "OpenAI Blog":     "https://openai.com/news/rss.xml",
    "Google DeepMind": "https://deepmind.google/blog/feed/basic/",
    "ArXiv cs.AI":     "https://rss.arxiv.org/rss/cs.AI",
    # Anthropic не публикует официальный RSS (anthropic.com/rss.xml -> 404).
    # Это community-зеркало claude.com/blog (обновляется ежедневно) — контент
    # первичный (слова самой Anthropic), канал доставки неофициальный.
    "Anthropic":       "https://tim-hilde.github.io/anthropic-rss/rss.xml",
}

# Новые источники — добавляются, если их ещё нет (ни по имени, ни по URL)
NEW_SOURCES = [
    {
        "name":     "Google Research",
        "url":      "https://research.google/blog/rss/",
        "category": "ai_models",
    },
]

# Источники без рабочей замены (Google Alerts, реальной альтернативы не нашлось) —
# деактивируем, не удаляем (сохраняем историю raw_articles по FK).
DEACTIVATE = ["DeepSeek", "The Batch"]


async def migrate():
    print("=== Миграция источников: Google Alerts → настоящие фиды ===\n")

    async with async_session_factory() as session:
        changes = 0

        for name, target_url in URL_UPDATES.items():
            result = await session.execute(
                text("SELECT id, url FROM sources WHERE name = :name ORDER BY id"),
                {"name": name},
            )
            rows = result.fetchall()

            if not rows:
                print(f"  [skip]    '{name}' — источник не найден в БД")
                continue

            correct = [r for r in rows if r[1] == target_url]
            stale   = [r for r in rows if r[1] != target_url]

            if correct and not stale:
                print(f"  [ok]      '{name}' — URL уже актуален")
                continue

            if correct and stale:
                for row in stale:
                    await session.execute(
                        text("DELETE FROM sources WHERE id = :id"),
                        {"id": row[0]},
                    )
                    print(f"  [dedup]   '{name}' — удалён старый дубликат (id={row[0]})")
                    changes += 1
                continue

            first_id = stale[0][0]
            await session.execute(
                text("UPDATE sources SET url = :url WHERE id = :id"),
                {"url": target_url, "id": first_id},
            )
            print(f"  [updated] '{name}' id={first_id} → {target_url}")
            changes += 1
            for row in stale[1:]:
                await session.execute(
                    text("DELETE FROM sources WHERE id = :id"),
                    {"id": row[0]},
                )
                print(f"  [dedup]   '{name}' — удалён лишний дубликат (id={row[0]})")
                changes += 1

        # Обновление URL источника не переписывает URL уже собранных статей —
        # старые статьи, полученные ЕЩЁ ЧЕРЕЗ Google Alerts, хранят его
        # редирект-ссылку (google.com/url?...) в raw_articles.url навсегда.
        # Такая ссылка ведёт себя плохо в Telegram-превью (не может вытащить
        # заголовок/картинку редиректа) — подтверждено на живом тесте.
        # Чистим их из пула кандидатов так же, как деактивированные источники.
        for name in list(URL_UPDATES) + DEACTIVATE:
            result = await session.execute(
                text(
                    "UPDATE raw_articles SET status = 'rejected' "
                    "WHERE status = 'new' AND url LIKE '%google.com/url%' "
                    "AND source_id IN (SELECT id FROM sources WHERE name = :name)"
                ),
                {"name": name},
            )
            if result.rowcount:
                print(f"  [cleanup]  '{name}' — {result.rowcount} старых статей с редирект-URL помечены rejected")
                changes += result.rowcount

        for src in NEW_SOURCES:
            result = await session.execute(
                text("SELECT id FROM sources WHERE name = :name OR url = :url"),
                {"name": src["name"], "url": src["url"]},
            )
            if result.fetchone():
                print(f"  [ok]      '{src['name']}' — уже существует")
                continue
            await session.execute(
                text(
                    "INSERT INTO sources (name, url, category, is_active, fetch_count) "
                    "VALUES (:name, :url, :category, 1, 0)"
                ),
                src,
            )
            print(f"  [added]   '{src['name']}' → {src['url']}")
            changes += 1

        for name in DEACTIVATE:
            result = await session.execute(
                text("SELECT id, is_active FROM sources WHERE name = :name"),
                {"name": name},
            )
            row = result.fetchone()
            if row is None:
                print(f"  [skip]    '{name}' — источник не найден в БД")
                continue
            if not row[1]:
                print(f"  [ok]      '{name}' — уже деактивирован")
            else:
                await session.execute(
                    text("UPDATE sources SET is_active = 0 WHERE id = :id"),
                    {"id": row[0]},
                )
                print(f"  [disabled] '{name}' id={row[0]} — деактивирован (нет замены с RSS)")
                changes += 1

            # Пул кандидатов researcher.py фильтрует только по raw_articles.status
            # и fetched_at — is_active источника НЕ проверяется. Без этой чистки
            # уже собранные статьи деактивированного источника ещё до 7 дней
            # продолжали бы участвовать в отборе кандидатов (подтверждено на
            # тесте: статья из отключённого DeepSeek-алерта попала в дайджест).
            result = await session.execute(
                text(
                    "UPDATE raw_articles SET status = 'rejected' "
                    "WHERE source_id = :sid AND status = 'new'"
                ),
                {"sid": row[0]},
            )
            if result.rowcount:
                print(f"  [cleanup]  '{name}' — {result.rowcount} статей в пуле помечены rejected")
                changes += result.rowcount

        await session.commit()

    print(f"\nГотово: {changes} изменений применено.")


if __name__ == "__main__":
    asyncio.run(migrate())
