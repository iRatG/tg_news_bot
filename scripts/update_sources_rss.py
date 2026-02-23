"""
Миграция RSS-источников: заменяет прямые URL на Google Alerts RSS.

Используется для обновления уже существующей БД на VPS (init_db.py не меняет
существующие записи, а лишь добавляет новые по URL).

Если init_db.py уже добавил Google Alerts записи как новые строки, скрипт удаляет
старые дублирующие записи по имени. Безопасно запускать многократно (идемпотентно).

Usage: python scripts/update_sources_rss.py
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

# Маппинг: имя источника → целевой URL (Google Alerts RSS)
URL_UPDATES = {
    "OpenAI Blog":     "https://www.google.com/alerts/feeds/12411355382602761870/7532254929191633487",
    "Anthropic":       "https://www.google.com/alerts/feeds/12411355382602761870/11509297532728669995",
    "Google DeepMind": "https://www.google.com/alerts/feeds/12411355382602761870/13699734839237282988",
    "ArXiv cs.AI":     "https://www.google.com/alerts/feeds/12411355382602761870/7048638059286736929",
    "The Batch":       "https://www.google.com/alerts/feeds/12411355382602761870/9850731068664068129",
}

# Новый источник — добавляется, если ещё нет
NEW_SOURCE = {
    "name":     "DeepSeek",
    "url":      "https://www.google.com/alerts/feeds/12411355382602761870/1744048258622321637",
    "category": "ai_models",
}


async def migrate():
    print("=== Миграция RSS-источников ===\n")

    async with async_session_factory() as session:
        changes = 0

        for name, target_url in URL_UPDATES.items():
            # Ищем все записи с этим именем
            result = await session.execute(
                text("SELECT id, url FROM sources WHERE name = :name ORDER BY id"),
                {"name": name},
            )
            rows = result.fetchall()

            if not rows:
                print(f"  [skip]    '{name}' — источник не найден в БД")
                continue

            # Находим записи с правильным URL и устаревшим URL
            correct = [r for r in rows if r[1] == target_url]
            stale   = [r for r in rows if r[1] != target_url]

            if correct and not stale:
                # Всё уже в порядке
                print(f"  [ok]      '{name}' — URL актуален")
                continue

            if correct and stale:
                # init_db.py уже добавил новую запись — удаляем старые дубликаты
                for row in stale:
                    await session.execute(
                        text("DELETE FROM sources WHERE id = :id"),
                        {"id": row[0]},
                    )
                    print(f"  [dedup]   '{name}' — удалён старый дубликат (id={row[0]}, url={row[1][:55]}...)")
                    changes += 1
                continue

            if not correct and stale:
                # Только старые записи — обновляем первую, остальные удаляем
                first_id = stale[0][0]
                await session.execute(
                    text("UPDATE sources SET url = :url WHERE id = :id"),
                    {"url": target_url, "id": first_id},
                )
                print(f"  [updated] '{name}' id={first_id} → {target_url[:55]}...")
                changes += 1
                for row in stale[1:]:
                    await session.execute(
                        text("DELETE FROM sources WHERE id = :id"),
                        {"id": row[0]},
                    )
                    print(f"  [dedup]   '{name}' — удалён лишний дубликат (id={row[0]})")
                    changes += 1

        # INSERT DeepSeek (если ещё нет — ни по имени, ни по URL)
        result = await session.execute(
            text("SELECT id FROM sources WHERE name = :name OR url = :url"),
            {"name": NEW_SOURCE["name"], "url": NEW_SOURCE["url"]},
        )
        if result.fetchone():
            print(f"  [ok]      '{NEW_SOURCE['name']}' — уже существует")
        else:
            await session.execute(
                text(
                    "INSERT INTO sources (name, url, category, is_active, fetch_interval_min) "
                    "VALUES (:name, :url, :category, 1, 25)"
                ),
                NEW_SOURCE,
            )
            print(f"  [added]   '{NEW_SOURCE['name']}' → {NEW_SOURCE['url'][:55]}...")
            changes += 1

        await session.commit()

    print(f"\nГотово: {changes} изменений применено.")


if __name__ == "__main__":
    asyncio.run(migrate())
