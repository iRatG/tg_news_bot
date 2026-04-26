#!/usr/bin/env python3
"""
Разовый прогон пайплайна — scripts/run_once.py.

Запускает один цикл: Researcher → Fact-Checker → Writer → Formatter → Analyst → Publish.
Пост уходит в канал, определяемый ENVIRONMENT в .env:
    ENVIRONMENT=development  → TELEGRAM_CHANNEL_TEST_ID  (@manitrus)
    ENVIRONMENT=production   → TELEGRAM_CHANNEL_ID       (@workhardatassp)

Использование:
    python scripts/run_once.py           # одиночный пост
    python scripts/run_once.py --digest  # утренний дайджест
    python scripts/run_once.py --arxiv   # arXiv прогон
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")


async def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else ""

    # Импортируем после добавления PROJECT_ROOT в sys.path
    from core.config import settings
    from core.pipeline import create_pipeline_run, run_pipeline, run_arxiv_pipeline

    channel = settings.active_channel
    env     = settings.ENVIRONMENT

    print(f"[run_once] ENVIRONMENT={env}")
    print(f"[run_once] Канал: {channel}")

    run_id = await create_pipeline_run()

    if mode == "--arxiv":
        print(f"[run_once] Режим: arXiv  (run_id={run_id})")
        await run_arxiv_pipeline(run_id)
    elif mode == "--digest":
        print(f"[run_once] Режим: дайджест  (run_id={run_id})")
        await run_pipeline(run_id, is_morning=True)
    else:
        print(f"[run_once] Режим: одиночный пост  (run_id={run_id})")
        await run_pipeline(run_id, is_morning=False)

    print(f"[run_once] Готово. Проверь {channel}")


if __name__ == "__main__":
    asyncio.run(main())
