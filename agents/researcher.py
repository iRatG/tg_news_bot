
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

MIN_SCORE          = 15  # Минимальный порог попадания в кандидаты
FALLBACK_MIN_SCORE = 6   # Fallback порог: когда пул старый, нет бонуса свежести
MAX_RESULTS        = 5   # Максимум кандидатов на выходе
FETCH_WORKERS      = 5   # Потоков для параллельного парсинга
INSERT_BATCH       = 100 # Размер батча INSERT (обход лимита SQLite в 999 переменных)
BRAND_CAP          = 2   # Макс. Tier 2/3 статей одного бренда в финальной выборке


# ── Детекция AI-бренда ────────────────────────────────────────────────────────

_BRAND_KEYWORDS: Dict[str, List[str]] = {
    "openai":     ["openai", "chatgpt", "chat gpt", "gpt-4", "gpt-3",
                   "gpt4", "gpt3", "sora", "dall-e", "dalle",
                   "o1 model", "o3 model"],
    "anthropic":  ["anthropic", "claude"],
    "google":     ["google deepmind", "google ai", "gemini", "deepmind",
                   "gemma", "vertex ai", "google bard"],
    "deepseek":   ["deepseek", "deep seek"],
    "meta":       ["meta ai", "meta llama", "llama 3", "llama-3", "llama3",
                   "llama 2", "llama-2", "llama2", "llama"],
    "perplexity": ["perplexity"],
    "mistral":    ["mistral", "mixtral"],
    "xai":        ["xai", "x.ai", "grok"],
}


def _detect_brand(title: str, content: str, url: str) -> str:
    """
    Определяет AI-бренд по заголовку, началу содержимого и URL.

    Проверяет бренды в порядке объявления в _BRAND_KEYWORDS.
    При совпадении нескольких брендов побеждает первый.
    Возвращает lowercase имя бренда или 'other'.
    """
    haystack = (title + " " + content[:500] + " " + url).lower()
    for brand, keywords in _BRAND_KEYWORDS.items():
        if any(kw in haystack for kw in keywords):
            return brand
    return "other"


# ── Детекция уровня важности (tier) ──────────────────────────────────────────

_TIER1_KEYWORDS = {
    # English
    "arxiv", "paper", "research", "benchmark", "study", "survey",
    "state-of-the-art", "sota", "outperforms", "architecture",
    "novel", "breakthrough", "preprint", "open-source", "open source",
    "weights", "dataset", "evaluation", "technical report",
    # Russian
    "исследование", "статья", "доклад", "бенчмарк", "архитектура",
    "разбор", "анализ", "открытие", "датасет",
}

_TIER3_KEYWORDS = {
    # English
    "funding", "investment", "valuation", "acquisition", "merger",
    "partnership", "deal", "ipo", "layoff", "layoffs", "hiring",
    "revenue", "profit", "series a", "series b", "series c",
    # Russian
    "инвестиции", "финансирование", "сделка", "партнерство",
    "поглощение", "слияние", "увольнения",
}

_TIER_MULT: Dict[str, float] = {
    "breakthrough": 2.0,   # прорывные статьи — всегда в приоритете
    "news":         1.0,   # обычные отраслевые новости
    "noise":        0.5,   # деловой шум — штраф
}


def _detect_tier(title: str, content: str) -> str:
    """
    Определяет уровень важности статьи.

    breakthrough — научная работа, бенчмарк, новая архитектура, открытие
    news         — отраслевая новость (выход модели, обновление сервиса)
    noise        — деловой шум (инвестиции, партнёрства, кадровые новости)

    Заголовок значимее содержимого (×2 вес).
    """
    title_lower   = title.lower()
    content_lower = content.lower() if content else ""

    t1 = (sum(2 for kw in _TIER1_KEYWORDS if kw in title_lower)
          + sum(1 for kw in _TIER1_KEYWORDS if kw in content_lower))
    t3 = (sum(2 for kw in _TIER3_KEYWORDS if kw in title_lower)
          + sum(1 for kw in _TIER3_KEYWORDS if kw in content_lower))

    if t1 >= 2:
        return "breakthrough"
    if t3 >= 2 and t1 == 0:
        return "noise"
    return "news"


# ── Выходная структура ────────────────────────────────────────────────────────

@dataclass
class RawArticleCandidate:
    """Статья-кандидат, прошедшая первичный отбор по score."""

    db_id:          int
    title:          str
    url:            str
    content:        str
    source_name:    str
    source_url:     str
    published_at:   datetime
    score:          int           # базовый score (ключевые слова + свежесть)
    category:       str   = ""
    brand:          str   = "other"   # AI-компания: openai/anthropic/google/…
    tier:           str   = "news"    # breakthrough/news/noise
    adjusted_score: float = 0.0       # score × tier_mult × diversity_mult

    def __repr__(self) -> str:
        return (
            f"<Candidate adj={self.adjusted_score:.1f} "
            f"tier={self.tier} brand={self.brand} "
            f"title={self.title[:50]!r}>"
        )


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


async def _get_brand_history(days: int = 7) -> Dict[str, int]:
    """
    Возвращает количество опубликованных постов по AI-брендам за последние N дней.

    Читает таблицу published_posts, детектирует бренд из post_text + source_url.
    При ошибке БД возвращает пустой словарь (нейтральное поведение для diversity_mult).
    """
    try:
        async with async_session_factory() as session:
            rows = (await session.execute(
                text("""
                    SELECT source_url, source_name, post_text
                    FROM published_posts
                    WHERE published_at > datetime('now', :window)
                """),
                {"window": f"-{days} days"},
            )).fetchall()
    except Exception as exc:
        logger.warning(f"[researcher] Не удалось прочитать историю брендов: {exc}")
        return {}

    brand_counts: Dict[str, int] = {}
    for source_url, source_name, post_text in rows:
        brand = _detect_brand(
            post_text[:300] if post_text else "",
            "",
            (source_url or "") + " " + (source_name or ""),
        )
        brand_counts[brand] = brand_counts.get(brand, 0) + 1
    return brand_counts


def _compute_diversity_mult(brand: str, brand_counts: Dict[str, int]) -> float:
    """
    Множитель разнообразия для бренда на основе его доли в последних публикациях.

    Формула: 1.0 + (1.0 − доля_бренда_за_7_дней)
    • Бренд никогда не публиковался (0%)  → mult = 2.0  (максимальный буст)
    • Бренд занимает 50% публикаций       → mult = 1.5
    • Бренд занимает 100% публикаций      → mult = 1.0  (нет штрафа)

    Доминирующий бренд не штрафуется — breakthrough-статья OpenAI всегда пройдёт.
    """
    total = sum(brand_counts.values())
    if total == 0:
        return 1.5  # нет истории → умеренный буст для всех
    brand_ratio = brand_counts.get(brand, 0) / total
    return round(1.0 + (1.0 - brand_ratio), 3)


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

    # 6. Скоринг: базовый score × tier_mult × diversity_mult
    brand_history = await _get_brand_history()
    logger.debug(f"[researcher] История брендов за 7 дней: {brand_history}")

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

        # Базовый score: ключевые слова + бонус свежести по fetched_at.
        # fetched_at корректнее RSS pub_at: старые блог-посты дают pub_at 2023/2024 → бонус 0.
        base_score = _compute_score(title, content, fetched_at_dt)

        if base_score >= MIN_SCORE:
            brand          = _detect_brand(title, content, url)
            tier           = _detect_tier(title, content)
            diversity_mult = _compute_diversity_mult(brand, brand_history)
            tier_mult      = _TIER_MULT[tier]
            adjusted_score = base_score * tier_mult * diversity_mult

            rss_pub = pub_index.get(url)
            scored.append(RawArticleCandidate(
                db_id=db_id,
                title=title,
                url=url,
                content=content[:3000],
                source_name=src_name,
                source_url=src_url,
                published_at=rss_pub or fetched_at_dt,
                score=base_score,
                category=category,
                brand=brand,
                tier=tier,
                adjusted_score=adjusted_score,
            ))

    # Fallback: если MIN_SCORE дал 0 кандидатов — снижаем порог до FALLBACK_MIN_SCORE.
    # Причина: пул состоит из старых статей (> 48ч) без бонуса свежести,
    # которые не набирают 15 только по ключевым словам.
    # Типичная ситуация: RSS вернул архивные посты (2017-2018) или нет новых лент.
    if not scored and pool_rows:
        logger.warning(
            f"[researcher] MIN_SCORE={MIN_SCORE} → 0 кандидатов. "
            f"Fallback: порог {FALLBACK_MIN_SCORE} (старый пул без бонуса свежести)."
        )
        for row in pool_rows:
            db_id, title, url, content, src_name, src_url, category, fetched_at_raw = row
            content = content or ""
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

            base_score = _compute_score(title, content, fetched_at_dt)
            if base_score < FALLBACK_MIN_SCORE:
                continue
            brand = _detect_brand(title, content, url)
            tier  = _detect_tier(title, content)
            adjusted_score = (
                base_score * _TIER_MULT[tier]
                * _compute_diversity_mult(brand, brand_history)
            )
            scored.append(RawArticleCandidate(
                db_id=db_id, title=title, url=url,
                content=content[:3000], source_name=src_name, source_url=src_url,
                published_at=pub_index.get(url) or fetched_at_dt,
                score=base_score, category=category, brand=brand, tier=tier,
                adjusted_score=adjusted_score,
            ))
        logger.warning(f"[researcher] Fallback дал {len(scored)} кандидатов.")

    scored.sort(key=lambda c: c.adjusted_score, reverse=True)

    # Soft cap: Tier 1 (breakthrough) всегда включается без ограничений.
    # Для Tier 2/3 — не более BRAND_CAP статей одного бренда в финальной выборке.
    brand_in_result: Dict[str, int] = {}
    candidates: List[RawArticleCandidate] = []
    for c in scored:
        if c.tier == "breakthrough":
            candidates.append(c)
        else:
            count = brand_in_result.get(c.brand, 0)
            if count < BRAND_CAP:
                candidates.append(c)
                brand_in_result[c.brand] = count + 1
        if len(candidates) >= MAX_RESULTS:
            break

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
    brand_summary = " ".join(f"{c.brand}({c.tier[0]})" for c in candidates)
    logger.info(
        f"[researcher] Готово: {len(candidates)}/{len(scored)} кандидатов "
        f"| пул={len(pool_rows)} | [{brand_summary}] | {elapsed_ms}мс"
    )
    for c in candidates:
        logger.debug(
            f"  [base={c.score:3d} adj={c.adjusted_score:5.1f} "
            f"tier={c.tier[0].upper()} brand={c.brand:12s}] "
            f"{c.source_name}: {c.title[:55]}"
        )

    return candidates
