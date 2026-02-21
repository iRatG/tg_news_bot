import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator

from sqlalchemy import create_engine, event
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Session, sessionmaker

from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./data/newsbot.db")

# Ensure the data directory exists before engine creation
_db_file = DATABASE_URL.replace("sqlite+aiosqlite:///", "")
Path(_db_file).parent.mkdir(parents=True, exist_ok=True)

# ── Async engine (used by the app) ────────────────────────────────────────────
engine = create_async_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
    echo=False,
)


@event.listens_for(engine.sync_engine, "connect")
def _set_sqlite_pragmas(dbapi_conn, connection_record):
    """Enable WAL mode and foreign-key enforcement on every new connection."""
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


async_session_factory = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


@asynccontextmanager
async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Async context manager — auto-commits on success, rolls back on error."""
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


# ── Sync engine (used by APScheduler jobstore only) ───────────────────────────
_sync_url = DATABASE_URL.replace("+aiosqlite", "")
sync_engine = create_engine(
    _sync_url,
    connect_args={"check_same_thread": False},
)

_SyncSession = sessionmaker(bind=sync_engine)


def get_db_sync() -> Session:
    """Synchronous session for APScheduler and other sync contexts."""
    return _SyncSession()
