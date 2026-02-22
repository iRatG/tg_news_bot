
"""
Агент 1 — Researcher.

Отвечает за сбор и первичную фильтрацию новостей из RSS-лент.
Не выполняет ни одного внешнего API-вызова — только локальная обработка данных.

Алгоритм:
    1. Загружает активные RSS-источники из БД.
    2. Параллельно парсит все ленты через ThreadPoolExecutor (max 5 потоков).
    3. Вычисляет score каждой статьи: ключевые слова + бонус за свежесть.
    4. Пакетно сохраняет новые статьи через INSERT OR IGNORE (батчи по 100).
    5. Возвращает топ-5 кандидатов с score >= 15.
       Пул ограничен статьями за последние 7 дней (fetched_at).
       fetched_at используется как proxy published_at для бонуса свежести.

Стоимость: $0.00 (без API-вызовов).
"""

import hashlib
import logging
import socket
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional

import feedparser
from sqlalchemy import text

from db.database import async_session_factory
from db.models import ArticleStatus

logger = logging.getLogger(__name__)


# ── Параметры скоринга ────────────────────────────────────────────────────────

_HIGH_VALUE: Dict[str, int] = {kw: 3 for kw in {
    "gpt", "claude", "gemini", "llm", "vibe coding", "ai agent",
    "cursor", "copilot", "mistral", "deepseek", "reasoning model",
    "multimodal", "openai", "anthropic", "model release", "benchmark",
}}
_MEDIUM_VALUE: Dict[str, int] = {kw: 2 for kw in {
    "machine learning", "neural network", "fine-tuning", "rag",
    "embeddings", "langchain", "langgraph", "crewai", "automation",
    "data engineering", "python ai", "prompt engineering",
}}
_LOW_VALUE: Dict[str, int] = {kw: 1 for kw in {
    "ai", "artificial intelligence", "data", "model",
    "tool", "api", "developer", "code",
}}

SCORE_WEIGHTS: Dict[str, int] = {**_HIGH_VALUE, **_MEDIUM_VALUE, **_LOW_VALUE}

# Бонус за свежесть: (порог в часах, баллы)
RECENCY_BONUSES = [(24, 20), (48, 10), (168, 5)]

MIN_SCORE    = 15   # Минимальный порог попадания в кандидаты
MAX_RESULTS  = 5    # Максимум кандидатов на выходе
FETCH_WORKERS = 5   # Потоков для параллельного парсинга
INSERT_BATCH  = 100 # Размер батча INSERT (обход лимита SQLite в 999 переменных)


# ── Выходная структура ────────────────────────────────────────────────────────

@dataclass
class RawArticleCandidate:
    """Статья-кандидат, прошедшая первичный отбор по score."""

    db_id:        int
    title:        str
    url:          str
    content:      str
    source_name:  str
    source_url:   str
    published_at: datetime
    score:        int
    category:     str = ""

    def __repr__(self) -> str:
        return f"<Candidate score={self.score} title={self.title[:60]!r}>"


# ── Вспомогательные функции ───────────────────────────────────────────────────

def _compute_score(title: str, content: str, published_at: Optional[datetime]) -> int:
    """
    Вычисляет релевантность статьи.

    Формула: Σ(вес × вхождений keyword) + бонус за свежесть.
    Поиск нечувствителен к регистру и ведётся по заголовку + содержимому.
    """
    haystack = (title + " " + content).lower()
    score = sum(weight * haystack.count(kw) for kw, weight in SCORE_WEIGHTS.items())

    if published_at is not None:
        if published_at.tzinfo is None:
            published_at = published_at.replace(tzinfo=timezone.utc)
        age_h = (datetime.now(timezone.utc) - published_at).total_seconds() / 3600
        for threshold, bonus in RECENCY_BONUSES:
            if age_h <= threshold:
                score += bonus
                break

    return score


def _title_md5(title: str) -> str:
    """MD5 нормализованного заголовка — ключ для дедупликации по заголовку."""
    return hashlib.md5(title.strip().lower().encode("utf-8")).hexdigest()


def _parse_feed(source: dict) -> List[dict]:
    """
    Синхронная загрузка одной RSS-ленты (запускается в ThreadPoolExecutor).

    Возвращает список сырых записей или пустой список при любой ошибке.
    feedparser безопасно работает даже с некорректным XML (bozo-режим),
    поэтому завершаем работу только при полном отсутствии записей.
    """
    try:
        # Таймаут 10с на сокет — feedparser не имеет встроенного таймаута
        socket.setdefaulttimeout(10)
        feed = feedparser.parse(
            source["url"],
            request_headers={"User-Agent": "Mozilla/5.0 tg-news-bot/1.0"},
        )
    except Exception as exc:
        logger.warning(f"[researcher] {source['name']}: ошибка загрузки — {exc}")
        return []

    if not feed.entries:
        if getattr(feed, "bozo", False):
            logger.warning(
                f"[researcher] {source['name']}: некорректный RSS — "
                f"{getattr(feed, 'bozo_exception', 'unknown')}"
            )
        return []

    articles = []
    for entry in feed.entries:
        title = (entry.get("title") or "").strip()
        url   = (entry.get("link")  or "").strip()
        if not title or not url:
            continue

        # Приоритет: summary → content[0].value → пусто
        content = ""
        if entry.get("summary"):
            content = entry["summary"]
        elif entry.get("content"):
            content = entry["content"][0].get("value", "")

        # Дата публикации
        pub: Optional[datetime] = None
        if entry.get("published_parsed"):
            try:
                pub = datetime(*entry["published_parsed"][:6], tzinfo=timezone.utc)
            except (TypeError, ValueError):
                pass

        articles.append({
            "source_id":    source["id"],
            "source_name":  source["name"],
            "source_url":   source["url"],
            "category":     source["category"],
            "title":        title,
            "url":          url,
            "content":      content,
            "published_at": pub,
            "title_md5":    _title_md5(title),
        })

    logger.debug(f"[researcher] {source['name']}: {len(articles)} записей")
    return articles


async def _bulk_insert(articles: List[dict]) -> None:
    """
    Пакетная вставка статей через INSERT OR IGNORE.

    Разбивает список на батчи по INSERT_BATCH во избежание превышения
    лимита SQLite на количество переменных в одном запросе (999).
    """
    async with async_session_factory() as session:
        for i in range(0, len(articles), INSERT_BATCH):
            batch = articles[i : i + INSERT_BATCH]
            for art in batch:
                try:
                    await session.execute(
                        text(
                            "INSERT OR IGNORE INTO raw_articles "
                            "(source_id, title, url, content, title_md5, status, retry_count) "
                            "VALUES (:source_id, :title, :url, :content, :title_md5, :status, 0)"
                        ),
                        {
                            "source_id": art["source_id"],
                            "title":     art["title"],
                            "url":       art["url"],
                            "content":   (art["content"] or "")[:5000],
                            "title_md5": art["title_md5"],
                            "status":    ArticleStatus.NEW,
                        },
                    )
                except Exception as exc:
                    # Единичный сбой не должен прерывать батч
                    logger.debug(f"[researcher] INSERT пропущен ({art['url'][:60]}): {exc}")
            await session.commit()


# ── Публичный интерфейс ───────────────────────────────────────────────────────

async def fetch_and_rank() -> List[RawArticleCandidate]:
    """
    Главная точка входа агента Researcher.

    Параллельно парсит RSS, сохраняет в БД и возвращает топ-кандидатов
    без единого платного API-вызова.

    Возвращает:
        Список до MAX_RESULTS статей (score >= MIN_SCORE),
        отсортированных по убыванию релевантности.
    """
    t0 = time.monotonic()

    # 1. Активные источники из БД
    async with async_session_factory() as session:
        rows = (await session.execute(
            text("SELECT id, name, url, category FROM sources WHERE is_active = 1")
        )).fetchall()

    sources = [{"id": r[0], "name": r[1], "url": r[2], "category": r[3]} for r in rows]
    if not sources:
        logger.warning("[researcher] Нет активных RSS-источников в БД")
        return []

    logger.info(f"[researcher] Парсинг {len(sources)} источников...")

    # 2. Параллельная загрузка RSS-лент
    all_raw: List[dict] = []
    with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
        futures = {pool.submit(_parse_feed, src): src["name"] for src in sources}
        for future in as_completed(futures, timeout=30):
            try:
                all_raw.extend(future.result())
            except Exception as exc:
                logger.warning(f"[researcher] Поток упал ({futures[future]}): {exc}")

    logger.info(f"[researcher] Получено из RSS: {len(all_raw)} записей")
    if not all_raw:
        return []

    # 3. Отсеиваем уже известные URL (batch-запрос без лимита параметров)
    async with async_session_factory() as session:
        existing_urls = {
            row[0]
            for row in (await session.execute(
                text("SELECT url FROM raw_articles")
            )).fetchall()
        }

    new_articles = [a for a in all_raw if a["url"] not in existing_urls]
    logger.info(f"[researcher] Новых статей для сохранения: {len(new_articles)}")

    # 4. Пакетная вставка новых статей
    if new_articles:
        await _bulk_insert(new_articles)

    # 5. Загружаем статьи со статусом 'new' за последние 7 дней как пул для скоринга.
    #    Фильтр по fetched_at отсекает зависшие старые статьи, которые никогда
    #    не пройдут скоринг без бонуса за свежесть.
    #    fetched_at включён в SELECT — используется как proxy published_at.
    async with async_session_factory() as session:
        pool_rows = (await session.execute(
            text("""
                SELECT ra.id, ra.title, ra.url, ra.content,
                       s.name, s.url, s.category, ra.fetched_at
                FROM raw_articles ra
                JOIN sources s ON ra.source_id = s.id
                WHERE ra.status = :status
                  AND ra.fetched_at > datetime('now', '-7 days')
                ORDER BY ra.fetched_at DESC
                LIMIT 200
            """),
            {"status": ArticleStatus.NEW},
        )).fetchall()

    # Индекс RSS-дат по url — используется ТОЛЬКО для поля published_at кандидата
    # (для отображения). Для скоринга RSS pub_at НЕ подходит: старые посты в RSS
    # имеют pub_at 2023-2024, что даёт нулевой бонус свежести.
    pub_index: Dict[str, Optional[datetime]] = {
        a["url"]: a["published_at"] for a in all_raw
    }

    # 6. Скоринг
    scored: List[RawArticleCandidate] = []
    for row in pool_rows:
        db_id, title, url, content, src_name, src_url, category, fetched_at_raw = row
        content = content or ""

        # Парсим fetched_at из SQLite (aiosqlite возвращает строку '2026-02-22 09:04:15')
        if isinstance(fetched_at_raw, str):
            try:
                fetched_at_dt = datetime.strptime(
                    fetched_at_raw[:19], "%Y-%m-%d %H:%M:%S"
                ).replace(tzinfo=timezone.utc)
            except ValueError:
                fetched_at_dt = datetime.now(timezone.utc)
        elif fetched_at_raw is None:
            fetched_at_dt = datetime.now(timezone.utc)
        else:
            fetched_at_dt = fetched_at_raw
            if fetched_at_dt.tzinfo is None:
                fetched_at_dt = fetched_at_dt.replace(tzinfo=timezone.utc)

        # Бонус свежести считаем по fetched_at — когда статья впервые появилась в нашем пуле.
        # Это корректнее RSS pub_at: старые блог-посты в RSS дают pub_at 2023/2024 → бонус 0.
        score = _compute_score(title, content, fetched_at_dt)

        if score >= MIN_SCORE:
            rss_pub = pub_index.get(url)
            scored.append(RawArticleCandidate(
                db_id=db_id,
                title=title,
                url=url,
                content=content[:3000],
                source_name=src_name,
                source_url=src_url,
                published_at=rss_pub or fetched_at_dt,
                score=score,
                category=category,
            ))

    scored.sort(key=lambda c: c.score, reverse=True)
    candidates = scored[:MAX_RESULTS]

    # 7. Обновляем статистику источников
    async with async_session_factory() as session:
        for src in sources:
            await session.execute(
                text(
                    "UPDATE sources "
                    "SET fetch_count = fetch_count + 1, "
                    "    last_fetched_at = datetime('now') "
                    "WHERE id = :id"
                ),
                {"id": src["id"]},
            )
        await session.commit()

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    logger.info(
        f"[researcher] Готово: {len(candidates)}/{len(scored)} кандидатов "
        f"| пул={len(pool_rows)} | {elapsed_ms}мс"
    )
    for c in candidates:
        logger.debug(f"  [{c.score:3d}] {c.source_name}: {c.title[:80]}")

    return candidates
