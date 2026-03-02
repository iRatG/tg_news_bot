"""
Миграция БД: добавить таблицу channel_stats_history — scripts/migrate_add_channel_stats.py.

Создаёт:
    - Таблицу channel_stats_history (ежедневные снапшоты числа подписчиков)

Безопасно запускать повторно (IF NOT EXISTS).

Использование:
    python scripts/migrate_add_channel_stats.py
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

# ── Таблица channel_stats_history ─────────────────────────────────────────────

cursor.execute("""
    CREATE TABLE IF NOT EXISTS channel_stats_history (
        date             TEXT PRIMARY KEY,     -- "2026-03-02"
        subscriber_count INTEGER NOT NULL,
        fetched_at       DATETIME NOT NULL DEFAULT (datetime('now'))
    )
""")
print("[migrate] channel_stats_history: OK")

conn.commit()
conn.close()

print("\n[migrate] Миграция завершена успешно.")
print(f"[migrate] БД: {DB_PATH}")
print("\nДалее:")
print("  1. Перезапустить сервис (scheduler подхватит новый cron snapshot)")
print("  2. Открыть /dashboard — первый snapshot запишется автоматически")
