"""SQLite cache and atomic per-day reservations shared across local workers."""

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from threading import Lock

from pm.settings import private_dir

_CACHE_LOCKS = tuple(Lock() for _ in range(64))
_ITEM_FIELDS = (
    "id", "kind", "ref", "title", "text", "url", "domain", "topic", "query",
    "product", "content_type", "retrieved_at",
)


def _cache_payload(raw):
    value = json.loads(raw)
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError("invalid_cache")
    items, warnings = value
    if (
        not isinstance(items, list) or not isinstance(warnings, list)
        or any(note != "result_filtered" for note in warnings)
        or any(
            not isinstance(item, dict)
            or not all(isinstance(item.get(field), str) for field in _ITEM_FIELDS)
            or not (item.get("published_at") is None or isinstance(item["published_at"], str))
            for item in items
        )
    ):
        raise ValueError("invalid_cache")
    return items, warnings


class Store:
    def __init__(self):
        path = private_dir() / "yandex-search.sqlite3"
        self.path = path.resolve()
        if path.is_symlink() or any((folder / ".git").exists() for folder in self.path.parents):
            raise ValueError("search_requires_private_directory")
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with closing(self._connect()) as db:
            version, = db.execute("PRAGMA user_version").fetchone()
            if version in {0, 1, 2}:
                db.executescript("""
                    BEGIN IMMEDIATE;
                    CREATE TABLE IF NOT EXISTS daily (
                        day TEXT PRIMARY KEY,
                        attempts INTEGER NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS cache (
                        key TEXT PRIMARY KEY,
                        fetched INTEGER NOT NULL,
                        payload TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS cache_fetched ON cache(fetched);
                    CREATE TABLE IF NOT EXISTS page_daily (
                        day TEXT PRIMARY KEY, attempts INTEGER NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS page_rate (
                        host TEXT PRIMARY KEY, scheduled REAL NOT NULL, gap REAL NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS page_cache (
                        url TEXT PRIMARY KEY, fetched REAL NOT NULL, payload TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS page_cache_fetched ON page_cache(fetched);
                    CREATE TABLE IF NOT EXISTS page_pause (
                        host TEXT PRIMARY KEY, until REAL NOT NULL
                    );
                    PRAGMA user_version = 3;
                    COMMIT;
                """)
            elif version != 3:
                raise ValueError("unsupported_store_version")

    def key_lock(self, key):
        return _CACHE_LOCKS[hash((self.path, key)) % len(_CACHE_LOCKS)]

    def _connect(self):
        return sqlite3.connect(self.path, timeout=2)

    def reserve(self, now, limit):
        today = now.astimezone(UTC).date()
        with closing(self._connect()) as db, db:
            cursor = db.execute("""
                INSERT INTO daily (day, attempts) VALUES (?, 1)
                ON CONFLICT(day) DO UPDATE SET attempts = attempts + 1
                WHERE attempts < ?
            """, (today.isoformat(), limit))
            if cursor.rowcount:
                db.execute("DELETE FROM daily WHERE day < ?", (
                    (today - timedelta(days=7)).isoformat(),
                ))
            return bool(cursor.rowcount)

    def page_slot(self, now, limit, host, delay):
        """Reserve a billed-independent crawl request and a shared per-host slot."""
        timestamp = now.timestamp()
        day = now.astimezone(UTC).date()
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            pause = db.execute("SELECT until FROM page_pause WHERE host = ?", (host,)).fetchone()
            if pause:
                timestamp = max(timestamp, pause[0])
            existing = db.execute(
                "SELECT scheduled, gap FROM page_rate WHERE host = ?", (host,),
            ).fetchone()
            if existing:
                previous, learned_gap = existing
                if now.timestamp() - previous < 86400:
                    delay = max(delay, learned_gap)
                timestamp = max(timestamp, previous + delay)
            wait = timestamp - now.timestamp()
            if delay > 3 or wait > 3:
                return None, "page_rate_deferred"
            cursor = db.execute("""
                INSERT INTO page_daily (day, attempts) VALUES (?, 1)
                ON CONFLICT(day) DO UPDATE SET attempts = attempts + 1
                WHERE attempts < ?
            """, (day.isoformat(), limit))
            if not cursor.rowcount:
                return None, "page_daily_limit"
            db.execute("""
                INSERT INTO page_rate (host, scheduled, gap) VALUES (?, ?, ?)
                ON CONFLICT(host) DO UPDATE SET scheduled=excluded.scheduled, gap=excluded.gap
            """, (host, timestamp, delay))
            db.execute("DELETE FROM page_daily WHERE day < ?", ((day - timedelta(days=7)).isoformat(),))
            db.execute("DELETE FROM page_rate WHERE scheduled < ?", (timestamp - 86400,))
            db.execute("DELETE FROM page_pause WHERE until <= ?", (now.timestamp(),))
            return wait, None

    def page_pause(self, now, host, seconds):
        with closing(self._connect()) as db, db:
            db.execute("""
                INSERT INTO page_pause(host, until) VALUES (?, ?)
                ON CONFLICT(host) DO UPDATE SET until=MAX(excluded.until, page_pause.until)
            """, (host, now.timestamp() + seconds))

    def page_get(self, key, now, ttl):
        if not ttl:
            return None
        timestamp = now.timestamp()
        with closing(self._connect()) as db:
            row = db.execute("""
                SELECT payload FROM page_cache WHERE url = ? AND fetched > ? AND fetched <= ?
            """, (key, timestamp - ttl, timestamp)).fetchone()
        return json.loads(row[0]) if row else None

    def page_put(self, key, value, now, ttl):
        if not ttl:
            return
        timestamp = now.timestamp()
        with closing(self._connect()) as db, db:
            db.execute("DELETE FROM page_cache WHERE fetched <= ?", (timestamp - ttl,))
            db.execute("""
                INSERT INTO page_cache (url, fetched, payload) VALUES (?, ?, ?)
                ON CONFLICT(url) DO UPDATE SET fetched=excluded.fetched, payload=excluded.payload
                WHERE excluded.fetched >= page_cache.fetched
            """, (key, timestamp, json.dumps(value, ensure_ascii=False)))

    def get(self, key, now, ttl):
        timestamp = now.timestamp()
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT fetched, payload FROM cache WHERE key = ?", (key,),
            ).fetchone()
        if not row:
            return None, False
        fetched, payload = row
        if not timestamp - ttl < fetched <= timestamp:
            return None, True
        return _cache_payload(payload), False

    def put(self, key, items, warnings, now, ttl):
        if not ttl:
            return
        with closing(self._connect()) as db, db:
            db.execute("DELETE FROM cache WHERE fetched <= ?", (now.timestamp() - ttl,))
            db.execute("""
                INSERT INTO cache (key, fetched, payload) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET fetched=excluded.fetched, payload=excluded.payload
                WHERE excluded.fetched >= cache.fetched
            """, (
                key, now.timestamp(), json.dumps([items, warnings], ensure_ascii=False),
            ))


def cache_key(folder, query, limit):
    # Increment the version when request or normalization semantics change.
    data = json.dumps([3, folder, query, limit], ensure_ascii=False)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def now_utc():
    return datetime.now(UTC)
