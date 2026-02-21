import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

load_dotenv()


def setup_logging() -> logging.Logger:
    """Configure rotating file + console logging. Call once at startup."""
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Rotating file: 5 MB × 3 files = 15 MB max
    file_handler = RotatingFileHandler(
        log_dir / "newsbot.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(getattr(logging, log_level, logging.INFO))

    # Avoid duplicate handlers on hot-reload
    if not root.handlers:
        root.addHandler(file_handler)
        root.addHandler(console_handler)

    # Silence noisy third-party loggers
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.INFO)

    return root


class AgentLogger:
    """Writes agent activity to both Python logging and the agent_logs DB table."""

    def __init__(self):
        self._log = logging.getLogger("agents")

    async def log_agent(
        self,
        agent_name: str,
        run_id: int,
        article_id: Optional[int],
        status: str,
        reason: Optional[str] = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        latency_ms: int = 0,
    ) -> None:
        from db.database import async_session_factory
        from db.models import AgentLog

        # 1. Persist to DB
        try:
            async with async_session_factory() as session:
                entry = AgentLog(
                    run_id=run_id,
                    article_id=article_id,
                    agent_name=agent_name,
                    status=status,
                    reason=reason,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    latency_ms=latency_ms,
                )
                session.add(entry)
                await session.commit()
        except Exception as exc:
            self._log.error(f"Failed to persist agent log to DB: {exc}")

        # 2. Write to Python logger
        parts = [f"[{agent_name}]", f"status={status}"]
        if article_id is not None:
            parts.append(f"article_id={article_id}")
        if reason:
            parts.append(f"reason={reason!r}")
        if latency_ms:
            parts.append(f"latency={latency_ms}ms")
        if input_tokens or output_tokens:
            parts.append(f"tokens={input_tokens}in+{output_tokens}out")

        msg = " ".join(parts)
        if status == "ok":
            self._log.info(msg)
        elif status == "rejected":
            self._log.warning(msg)
        else:
            self._log.error(msg)


# Singleton — import from here in other modules
agent_logger = AgentLogger()
