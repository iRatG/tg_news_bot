"""
First-run script: creates all DB tables and seeds default data.
Usage: python scripts/init_db.py
Safe to run multiple times (idempotent — uses INSERT OR IGNORE logic).
"""

import asyncio
import sys
from pathlib import Path

# Force UTF-8 output on Windows
if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

# Allow imports from project root
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

from sqlalchemy import text
from db.database import engine, async_session_factory
from db.models import Base, Source, Setting, ScheduleSlot

# ── Seed data ─────────────────────────────────────────────────────────────────

RSS_SEEDS = [
    # Google Alerts RSS — заменяют прямые фиды, которые падают по SSL-timeout на RU VPS
    {"name": "OpenAI Blog",          "url": "https://www.google.com/alerts/feeds/12411355382602761870/7532254929191633487",   "category": "ai_models"},
    {"name": "Anthropic",            "url": "https://www.google.com/alerts/feeds/12411355382602761870/11509297532728669995",  "category": "ai_models"},
    {"name": "Google DeepMind",      "url": "https://www.google.com/alerts/feeds/12411355382602761870/13699734839237282988",  "category": "ai_models"},
    {"name": "DeepSeek",             "url": "https://www.google.com/alerts/feeds/12411355382602761870/1744048258622321637",   "category": "ai_models"},
    {"name": "ArXiv cs.AI",          "url": "https://www.google.com/alerts/feeds/12411355382602761870/7048638059286736929",   "category": "research"},
    {"name": "The Batch",            "url": "https://www.google.com/alerts/feeds/12411355382602761870/9850731068664068129",   "category": "ai_news"},
    # Прямые фиды (стабильны на RU VPS)
    {"name": "HuggingFace Blog",     "url": "https://huggingface.co/blog/feed.xml",          "category": "ai_models"},
    {"name": "Simon Willison",       "url": "https://simonwillison.net/atom/entries/",        "category": "vibe_coding"},
    {"name": "Latent Space",         "url": "https://www.latent.space/feed",                  "category": "vibe_coding"},
    {"name": "TLDR AI",              "url": "https://tldr.tech/ai/rss",                      "category": "ai_news"},
    {"name": "Towards Data Science", "url": "https://towardsdatascience.com/feed",            "category": "data_eng"},
]

WRITER_SYSTEM_PROMPT = """Ты — senior data engineer и AI practitioner с 10 годами опыта.
Пишешь пост для Telegram-канала об AI, LLM и vibe coding.

Структура поста (строго):
1. Эмодзи + Заголовок — 1 предложение, суть без воды
2. Что случилось — 2-3 предложения фактически
3. Почему это важно для нас — 1-2 предложения:
   что изменится в работе data engineer / AI-разработчика,
   какой инструмент устареет, что стоит попробовать прямо сейчас
4. 🔗 Источник: [название](url)

Язык: русский. Тон: умный коллега, не журналист.
Длина: 400-600 символов. Без "в заключении". Без воды."""

DEFAULT_SETTINGS = [
    ("posts_per_day",       "3",                     "Number of posts published per day"),
    ("max_candidates",      "5",                     "Max RSS candidates evaluated per pipeline run"),
    ("min_fact_check_conf", "0.65",                  "Minimum Perplexity confidence score to pass fact-check"),
    ("dedup_threshold",     "0.80",                  "Cosine similarity threshold for semantic deduplication"),
    ("dedup_lookback_days", "30",                    "Days to look back when checking for duplicate topics"),
    ("language",            "ru",                    "Post language"),
    ("image_enabled",       "false",                 "Enable Leonardo AI image generation (true/false)"),
    ("image_style",         "futuristic digital art, no text", "Leonardo AI image style prompt"),
    ("posts_per_run",       "1",                     "Number of posts to publish per single pipeline run"),
    ("writer_system_prompt", WRITER_SYSTEM_PROMPT,   "System prompt for the Writer agent"),
    ("post_style_current",  "curator",               "Текущий стиль постов: curator|tech_analyst|practitioner|skeptic"),
    ("morning_digest_hour", "7",                     "Час утреннего дайджеста (МСК, 0-23)"),
    ("morning_digest_enabled", "true",               "Включить утренний дайджест (true/false)"),
]

DEFAULT_SCHEDULE_SLOTS = [
    (9,  0, "mon-sun"),
    (14, 0, "mon-sun"),
    (19, 0, "mon-sun"),
]

# ── Init logic ────────────────────────────────────────────────────────────────

async def init_db():
    print("=== NewsBot DB Initialization ===\n")

    # Step 1: create all tables
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    print("✓ Tables created (or already exist)")

    async with async_session_factory() as session:

        # Step 2: seed RSS sources
        sources_added = 0
        for seed in RSS_SEEDS:
            result = await session.execute(
                text("SELECT id FROM sources WHERE url = :url"),
                {"url": seed["url"]},
            )
            if not result.fetchone():
                session.add(Source(**seed))
                sources_added += 1
        await session.flush()

        # Step 3: seed default settings
        settings_added = 0
        for key, value, description in DEFAULT_SETTINGS:
            result = await session.execute(
                text("SELECT key FROM settings WHERE key = :key"),
                {"key": key},
            )
            if not result.fetchone():
                session.add(Setting(key=key, value=value, description=description))
                settings_added += 1
        await session.flush()

        # Step 4: seed default schedule slots
        slots_added = 0
        for hour, minute, days in DEFAULT_SCHEDULE_SLOTS:
            result = await session.execute(
                text(
                    "SELECT id FROM schedule_slots "
                    "WHERE hour = :hour AND minute = :minute"
                ),
                {"hour": hour, "minute": minute},
            )
            if not result.fetchone():
                session.add(
                    ScheduleSlot(
                        hour=hour,
                        minute=minute,
                        days_of_week=days,
                        is_active=True,
                    )
                )
                slots_added += 1

        await session.commit()

    print(f"✓ RSS sources:     {sources_added} added (total seeds: {len(RSS_SEEDS)})")
    print(f"✓ Settings:        {settings_added} added (total defaults: {len(DEFAULT_SETTINGS)})")
    print(f"✓ Schedule slots:  {slots_added} added (total defaults: {len(DEFAULT_SCHEDULE_SLOTS)})")
    print("\nDB initialized successfully. Ready to start the bot.")


if __name__ == "__main__":
    asyncio.run(init_db())
