from datetime import datetime
from enum import Enum
from typing import List, Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.ext.asyncio import AsyncAttrs
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.sql import func


class ArticleStatus(str, Enum):
    NEW = "new"
    PROCESSING = "processing"
    PUBLISHED = "published"
    REJECTED = "rejected"
    FAILED = "failed"


class RunStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    COMPLETED_EMPTY = "completed_empty"
    FAILED = "failed"


class Base(AsyncAttrs, DeclarativeBase):
    pass


class Source(Base):
    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    url: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    category: Mapped[str] = mapped_column(String, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="1")
    fetch_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    last_fetched_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )

    articles: Mapped[List["RawArticle"]] = relationship(
        "RawArticle", back_populates="source"
    )

    def __repr__(self) -> str:
        return f"<Source(id={self.id}, name={self.name!r}, category={self.category!r})>"


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:
        return f"<Setting(key={self.key!r}, value={self.value!r})>"


class ScheduleSlot(Base):
    __tablename__ = "schedule_slots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    hour: Mapped[int] = mapped_column(Integer, nullable=False)
    minute: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    days_of_week: Mapped[str] = mapped_column(
        String, nullable=False, default="mon-sun"
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    __table_args__ = (
        CheckConstraint("hour >= 0 AND hour <= 23", name="check_hour"),
        CheckConstraint("minute >= 0 AND minute <= 59", name="check_minute"),
    )

    def __repr__(self) -> str:
        return f"<ScheduleSlot(id={self.id}, hour={self.hour:02d}:{self.minute:02d})>"


class RawArticle(Base):
    __tablename__ = "raw_articles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("sources.id"), nullable=False
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    content: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    title_md5: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    status: Mapped[str] = mapped_column(
        String, nullable=False, default=ArticleStatus.NEW
    )
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    source: Mapped["Source"] = relationship("Source", back_populates="articles")
    embedding: Mapped[Optional["ArticleEmbedding"]] = relationship(
        "ArticleEmbedding", back_populates="article", uselist=False
    )
    agent_logs: Mapped[List["AgentLog"]] = relationship(
        "AgentLog", back_populates="article"
    )
    published_post: Mapped[Optional["PublishedPost"]] = relationship(
        "PublishedPost", back_populates="article", uselist=False
    )

    def __repr__(self) -> str:
        return f"<RawArticle(id={self.id}, title={self.title[:50]!r}, status={self.status!r})>"


class ArticleEmbedding(Base):
    __tablename__ = "article_embeddings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    article_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("raw_articles.id"), nullable=False
    )
    embedding: Mapped[str] = mapped_column(Text, nullable=False)  # JSON array of 1536 floats
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )

    article: Mapped["RawArticle"] = relationship(
        "RawArticle", back_populates="embedding"
    )

    def __repr__(self) -> str:
        return f"<ArticleEmbedding(id={self.id}, article_id={self.article_id})>"


class PipelineRun(Base):
    __tablename__ = "pipeline_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    articles_found: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    articles_verified: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    articles_published: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    status: Mapped[str] = mapped_column(
        String, nullable=False, default=RunStatus.RUNNING, server_default="running"
    )
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    agent_logs: Mapped[List["AgentLog"]] = relationship(
        "AgentLog", back_populates="run"
    )
    published_posts: Mapped[List["PublishedPost"]] = relationship(
        "PublishedPost", back_populates="run"
    )

    def __repr__(self) -> str:
        return f"<PipelineRun(id={self.id}, status={self.status!r})>"


class AgentLog(Base):
    __tablename__ = "agent_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("pipeline_runs.id"), nullable=False
    )
    article_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("raw_articles.id"), nullable=True
    )
    agent_name: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    latency_ms: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )

    run: Mapped["PipelineRun"] = relationship("PipelineRun", back_populates="agent_logs")
    article: Mapped[Optional["RawArticle"]] = relationship(
        "RawArticle", back_populates="agent_logs"
    )

    def __repr__(self) -> str:
        return f"<AgentLog(id={self.id}, agent={self.agent_name!r}, status={self.status!r})>"


class PublishedPost(Base):
    __tablename__ = "published_posts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    article_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("raw_articles.id"), nullable=False
    )
    run_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("pipeline_runs.id"), nullable=False
    )
    telegram_msg_id: Mapped[int] = mapped_column(Integer, nullable=False)
    channel_id: Mapped[str] = mapped_column(String, nullable=False)
    post_text: Mapped[str] = mapped_column(Text, nullable=False)
    source_url: Mapped[str] = mapped_column(String, nullable=False)
    source_name: Mapped[str] = mapped_column(String, nullable=False)
    has_image: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    published_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )

    article: Mapped["RawArticle"] = relationship(
        "RawArticle", back_populates="published_post"
    )
    run: Mapped["PipelineRun"] = relationship(
        "PipelineRun", back_populates="published_posts"
    )
    stats: Mapped[List["PostStats"]] = relationship("PostStats", back_populates="post")

    def __repr__(self) -> str:
        return f"<PublishedPost(id={self.id}, telegram_msg_id={self.telegram_msg_id})>"


class PostStats(Base):
    __tablename__ = "post_stats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    post_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("published_posts.id"), nullable=False
    )
    views: Mapped[int] = mapped_column(Integer, default=0)
    forwards: Mapped[int] = mapped_column(Integer, default=0)
    reactions: Mapped[int] = mapped_column(Integer, default=0)
    collected_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )

    post: Mapped["PublishedPost"] = relationship("PublishedPost", back_populates="stats")

    def __repr__(self) -> str:
        return f"<PostStats(id={self.id}, post_id={self.post_id}, views={self.views})>"


class ChannelStatsHistory(Base):
    """Ежедневный snapshot числа подписчиков Telegram-канала."""

    __tablename__ = "channel_stats_history"

    date: Mapped[str] = mapped_column(String, primary_key=True)  # "2026-03-02"
    subscriber_count: Mapped[int] = mapped_column(Integer, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:
        return f"<ChannelStatsHistory(date={self.date!r}, count={self.subscriber_count})>"


class ArxivSeenPaper(Base):
    """Таблица для дедупликации бумаг arXiv (заменяет seen_papers.json)."""

    __tablename__ = "arxiv_seen_papers"

    arxiv_id: Mapped[str] = mapped_column(String, primary_key=True)  # "2502.12345"
    title: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:
        return f"<ArxivSeenPaper(arxiv_id={self.arxiv_id!r})>"
