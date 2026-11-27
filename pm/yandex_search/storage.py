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
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = path.resolve()
        with closing(self._connect()) as db:
            version, = db.execute("PRAGMA user_version").fetchone()
            if version == 0:
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
                    PRAGMA user_version = 1;
                    COMMIT;
                """)
            elif version != 1:
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

    def get(self, key, now, ttl):
        if not ttl:
            return None
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT payload FROM cache WHERE key = ? AND fetched > ? AND fetched <= ?",
                (key, now.timestamp() - ttl, now.timestamp()),
            ).fetchone()
        return _cache_payload(row[0]) if row else None

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
    data = json.dumps([2, folder, query, limit], ensure_ascii=False)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def now_utc():
    return datetime.now(UTC)
