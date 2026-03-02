"""
Миграция источников RSS — scripts/migrate_update_sources.py.

Заменяет нерабочие источники на надёжные:
    - TLDR AI (tldr.tech SSL timeout) → The Verge AI
    - Добавляет VentureBeat AI (разнообразие)
    - Добавляет MIT Technology Review (авторитетный источник)

Безопасно запускать повторно (INSERT OR IGNORE / UPDATE by URL).

Использование:
    python scripts/migrate_update_sources.py
"""

import os
import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "data", "newsbot.db")

if not os.path.exists(DB_PATH):
    print(f"[migrate] ОШИБКА: БД не найдена: {DB_PATH}")
    sys.exit(1)

conn = sqlite3.connect(DB_PATH)
cursor = conn.cursor()

# ── 1. Отключить TLDR AI (SSL timeout на VPS) ─────────────────────────────────

cursor.execute("""
    UPDATE sources SET is_active = 0
    WHERE url = 'https://tldr.tech/ai/rss'
""")
if cursor.rowcount:
    print("[migrate] sources: TLDR AI отключён (SSL timeout)")
else:
    print("[migrate] sources: TLDR AI не найден — пропущено")

# ── 2. Добавить новые источники ────────────────────────────────────────────────

new_sources = [
    (
        "The Verge AI",
        "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml",
        "news",
    ),
    (
        "VentureBeat AI",
        "https://venturebeat.com/category/ai/feed/",
        "news",
    ),
    (
        "MIT Technology Review",
        "https://www.technologyreview.com/feed/",
        "research",
    ),
]

for name, url, category in new_sources:
    cursor.execute("""
        INSERT OR IGNORE INTO sources (name, url, category, is_active, fetch_count)
        VALUES (?, ?, ?, 1, 0)
    """, (name, url, category))
    if cursor.rowcount:
        print(f"[migrate] sources: добавлен '{name}'")
    else:
        # Если уже есть — убедимся что активен
        cursor.execute("UPDATE sources SET is_active = 1 WHERE url = ?", (url,))
        print(f"[migrate] sources: '{name}' уже существует — активирован")

# ── Итог ──────────────────────────────────────────────────────────────────────

print()
cursor.execute("SELECT id, name, is_active, fetch_count FROM sources ORDER BY id")
for r in cursor.fetchall():
    status = "✓" if r[2] else "✗"
    print(f"  [{status}] id={r[0]:2d} fetch={r[3]:3d}  {r[1]}")

conn.commit()
conn.close()

print("\n[migrate] Миграция источников завершена.")
