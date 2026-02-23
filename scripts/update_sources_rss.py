"""
Миграция RSS-источников: заменяет прямые URL на Google Alerts RSS.

Используется для обновления уже существующей БД на VPS (init_db.py не меняет
существующие записи, а лишь добавляет новые по URL).

Безопасно запускать многократно (идемпотентно).
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

# Маппинг: имя источника → новый URL (Google Alerts RSS)
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

        # 1. UPDATE существующих источников
        updated = 0
        for name, new_url in URL_UPDATES.items():
            result = await session.execute(
                text("SELECT id, url FROM sources WHERE name = :name"),
                {"name": name},
            )
            row = result.fetchone()
            if row is None:
                print(f"  [skip]    '{name}' — источник не найден в БД")
                continue
            if row[1] == new_url:
                print(f"  [ok]      '{name}' — URL уже актуален")
                continue
            await session.execute(
                text("UPDATE sources SET url = :url WHERE name = :name"),
                {"url": new_url, "name": name},
            )
            print(f"  [updated] '{name}' → {new_url[:60]}...")
            updated += 1

        # 2. INSERT DeepSeek (если ещё нет)
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
            print(f"  [added]   '{NEW_SOURCE['name']}' → {NEW_SOURCE['url'][:60]}...")
            updated += 1

        await session.commit()

    print(f"\nГотово: {updated} изменений применено.")


if __name__ == "__main__":
    asyncio.run(migrate())
