"""
Миграция БД: добавить поддержку arXiv агента — scripts/migrate_add_arxiv_seen.py.

Создаёт:
    - Таблицу arxiv_seen_papers (дедупликация, заменяет seen_papers.json)
    - Источник 'arXiv API' в таблице sources (нужен как FK для raw_articles)
    - Настройки arxiv_schedule_* в таблице settings

Безопасно запускать повторно (IF NOT EXISTS / INSERT OR IGNORE).

Использование:
    python scripts/migrate_add_arxiv_seen.py
"""

import os
import sqlite3
import sys

# Запуск из корня проекта или из папки scripts
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "data", "newsbot.db")

if not os.path.exists(DB_PATH):
    print(f"[migrate] ОШИБКА: БД не найдена: {DB_PATH}")
    sys.exit(1)

conn = sqlite3.connect(DB_PATH)
cursor = conn.cursor()

# ── 1. Таблица arxiv_seen_papers ──────────────────────────────────────────────

cursor.execute("""
    CREATE TABLE IF NOT EXISTS arxiv_seen_papers (
        arxiv_id     TEXT PRIMARY KEY,
        title        TEXT,
        first_seen_at DATETIME NOT NULL DEFAULT (datetime('now'))
    )
""")
print("[migrate] arxiv_seen_papers: OK")

# ── 2. Источник 'arXiv API' в sources ─────────────────────────────────────────

cursor.execute("""
    INSERT OR IGNORE INTO sources (name, url, category, is_active, fetch_count)
    VALUES ('arXiv API', 'https://arxiv.org/api/', 'research', 1, 0)
""")
if cursor.rowcount:
    print("[migrate] sources: добавлен источник 'arXiv API'")
else:
    print("[migrate] sources: источник 'arXiv API' уже существует")

# ── 3. Настройки планировщика arXiv ───────────────────────────────────────────

settings_rows = [
    ("arxiv_schedule_enabled", "true",
     "Включить автоматический запуск arXiv агента (true/false)"),
    ("arxiv_schedule_hour", "18",
     "Час ежедневного запуска arXiv агента (МСК, 0-23)"),
    ("arxiv_max_papers", "1",
     "Максимальное количество бумаг за один arXiv прогон"),
]

for key, value, description in settings_rows:
    cursor.execute("""
        INSERT OR IGNORE INTO settings (key, value, description)
        VALUES (?, ?, ?)
    """, (key, value, description))
    if cursor.rowcount:
        print(f"[migrate] settings: добавлена настройка '{key}' = '{value}'")
    else:
        print(f"[migrate] settings: '{key}' уже существует — пропущено")

conn.commit()
conn.close()

print("\n[migrate] Миграция завершена успешно.")
print(f"[migrate] БД: {DB_PATH}")
print("\nДалее:")
print("  1. Перезапустить сервис (reload_schedule подхватит arxiv cron)")
print("  2. Тест: POST /api/pipeline/run_arxiv")
