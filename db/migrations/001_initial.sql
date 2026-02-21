-- db/migrations/001_initial.sql
-- Reference SQL schema (tables are created via SQLAlchemy in production)
-- SQLite with WAL mode for concurrent reads

PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- RSS Sources
CREATE TABLE IF NOT EXISTS sources (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    url             TEXT NOT NULL UNIQUE,
    category        TEXT NOT NULL,
    is_active       INTEGER NOT NULL DEFAULT 1,
    fetch_count     INTEGER NOT NULL DEFAULT 0,
    last_fetched_at DATETIME,
    created_at      DATETIME NOT NULL DEFAULT (datetime('now'))
);

-- System settings (key-value config)
CREATE TABLE IF NOT EXISTS settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    description TEXT,
    updated_at  DATETIME NOT NULL DEFAULT (datetime('now'))
);

-- Posting schedule
CREATE TABLE IF NOT EXISTS schedule_slots (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    hour         INTEGER NOT NULL CHECK(hour >= 0 AND hour <= 23),
    minute       INTEGER NOT NULL DEFAULT 0 CHECK(minute >= 0 AND minute <= 59),
    days_of_week TEXT NOT NULL DEFAULT 'mon-sun',
    is_active    INTEGER NOT NULL DEFAULT 1
);

-- Raw articles from RSS (before processing)
CREATE TABLE IF NOT EXISTS raw_articles (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id   INTEGER NOT NULL REFERENCES sources(id),
    title       TEXT NOT NULL,
    url         TEXT NOT NULL UNIQUE,
    content     TEXT,
    title_md5   TEXT NOT NULL UNIQUE,
    fetched_at  DATETIME NOT NULL DEFAULT (datetime('now')),
    status      TEXT NOT NULL DEFAULT 'new',
    retry_count INTEGER NOT NULL DEFAULT 0
);

-- Embeddings for semantic deduplication
CREATE TABLE IF NOT EXISTS article_embeddings (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    article_id INTEGER NOT NULL REFERENCES raw_articles(id),
    embedding  TEXT NOT NULL,
    created_at DATETIME NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_embeddings_article ON article_embeddings(article_id);

-- Pipeline run metadata
CREATE TABLE IF NOT EXISTS pipeline_runs (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at         DATETIME NOT NULL DEFAULT (datetime('now')),
    finished_at        DATETIME,
    articles_found     INTEGER DEFAULT 0,
    articles_verified  INTEGER DEFAULT 0,
    articles_published INTEGER DEFAULT 0,
    status             TEXT NOT NULL DEFAULT 'running',
    error_message      TEXT
);

-- Per-agent step logs
CREATE TABLE IF NOT EXISTS agent_logs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       INTEGER NOT NULL REFERENCES pipeline_runs(id),
    article_id   INTEGER REFERENCES raw_articles(id),
    agent_name   TEXT NOT NULL,
    status       TEXT NOT NULL,
    reason       TEXT,
    input_tokens  INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    latency_ms    INTEGER DEFAULT 0,
    created_at   DATETIME NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_agent_logs_run   ON agent_logs(run_id);
CREATE INDEX IF NOT EXISTS idx_agent_logs_agent ON agent_logs(agent_name, status);

-- Published posts
CREATE TABLE IF NOT EXISTS published_posts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    article_id      INTEGER NOT NULL REFERENCES raw_articles(id),
    run_id          INTEGER NOT NULL REFERENCES pipeline_runs(id),
    telegram_msg_id INTEGER NOT NULL,
    channel_id      TEXT NOT NULL,
    post_text       TEXT NOT NULL,
    source_url      TEXT NOT NULL,
    source_name     TEXT NOT NULL,
    has_image       INTEGER NOT NULL DEFAULT 0,
    published_at    DATETIME NOT NULL DEFAULT (datetime('now'))
);

-- Post engagement stats
CREATE TABLE IF NOT EXISTS post_stats (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id      INTEGER NOT NULL REFERENCES published_posts(id),
    views        INTEGER DEFAULT 0,
    forwards     INTEGER DEFAULT 0,
    reactions    INTEGER DEFAULT 0,
    collected_at DATETIME NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_post_stats_post ON post_stats(post_id);
