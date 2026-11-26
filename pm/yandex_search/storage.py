"""SQLite cache and atomic per-day reservations shared across local workers."""

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta

from pm.settings import private_dir


class Store:
    def __init__(self):
        path = private_dir() / "yandex-search.sqlite3"
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = path
        with closing(self._connect()) as db, db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS daily (
                    day TEXT PRIMARY KEY,
                    attempts INTEGER NOT NULL
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS cache (
                    key TEXT PRIMARY KEY,
                    fetched INTEGER NOT NULL,
                    payload TEXT NOT NULL
                )
            """)

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
        return json.loads(row[0]) if row else None

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
    data = json.dumps([1, folder, query, limit], ensure_ascii=False)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def now_utc():
    return datetime.now(UTC)
