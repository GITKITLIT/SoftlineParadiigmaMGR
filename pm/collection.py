"""Bounded deterministic ingestion and periodic collection."""
import hashlib
import os
import sqlite3
import threading
from datetime import date, datetime, timedelta, timezone

from . import ai_store, memory
from .settings import ROOT


def interval():
    value = int(os.getenv("PM_COLLECTION_INTERVAL", "0") or "0")
    if value and not 300 <= value <= 86400:
        raise ValueError("Интервал памяти должен быть 0 либо от 300 до 86400 секунд")
    return value


def capture_sales(snapshot, mode):
    source = os.getenv("PM_SALES_SOURCE_ID", "sales_export") if mode == "local" else "demo_sales"
    version = os.getenv("PM_SALES_METHOD", "weekly-index-v1") if mode == "local" else "weekly-index-v1"
    zone = os.getenv("PM_SALES_TIMEZONE", "Europe/Moscow")
    rows = []
    for series in snapshot["series"]:
        for start, weekly, rolling in series["points"]:
            start = memory.iso(start)
            end = (date.fromisoformat(start) + timedelta(days=6)).isoformat()
            for metric, value, aggregation in (
                ("orders_index", weekly, "mean"), ("orders_index_4w", rolling, "exact"),
            ):
                rows.append(dict(
                    stream="sales", mode=mode, source=source, product=series["name"], metric=metric,
                    unit="index", method=version, segment=snapshot.get("channel"), timezone=zone,
                    aggregation=aggregation, start=start, end=end, value=value,
                    quality="partial" if start >= snapshot["config"]["partial_week"] else "complete",
                ))
    releases = []
    if snapshot.get("launch") and snapshot.get("release_id"):
        releases.append({"id": snapshot["release_id"], "date": snapshot["launch"],
                         "related_task": snapshot.get("release_task")})
    releases.extend(snapshot.get("release_history", []))
    for item in releases:
        if snapshot["series"]:
            rows.append(dict(stream="release", mode=mode, source=source,
                             product=snapshot["series"][0]["name"], metric="release",
                             unit="event", subject=item["id"], aggregation="snapshot",
                             start=item["date"], event_date=item["date"], quality="complete",
                             metadata={"related_task": item.get("related_task"), "title": item.get("title")}))
    return memory.write_observations(rows)


def save_meter(result, counter, filter_expression, *, definitions=None, coverage="store_counter"):
    date_end = result["period"]["end"]
    already_closed = date_end < date.today().isoformat()
    rows = []
    for name, metric in result["metrics"].items():
        method = (definitions or {}).get(name, (metric["metric"],))[0]
        rows.append(dict(
            stream="metrika", mode="local" if coverage == "store_counter" else "demo_counter",
            source=f"counter:{counter}", product=os.getenv("PM_METRIKA_PRODUCT", "store"),
            metric=name, method=method, unit=metric["unit"], segment=result.get("segment"),
            timezone=os.getenv("METRIKA_COUNTER_TIMEZONE", "Europe/Moscow"),
            aggregation="sum" if metric["unit"] in {"visits", "views", "completions", "orders"} else "exact",
            start=result["period"]["start"], end=date_end, retrieved=result["retrieved"], value=metric["value"],
            quality=("sampled" if result["sampled"] else "complete" if already_closed else "partial"),
            dimensions={"filter_fingerprint": memory.digest(filter_expression or ""), "coverage": coverage},
        ))
    return memory.write_observations(rows)


def ingest_file(path, *, mode):
    import json
    from pathlib import Path
    path = Path(path).resolve()
    if not path.is_file() or path.stat().st_size > 5_000_000:
        raise ValueError("Выберите локальный JSON с наблюдениями до 5 МБ")
    if mode not in memory.MODES:
        raise ValueError("Укажите режим local или synthetic")
    rows = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(rows, list):
        raise ValueError("Ожидается массив наблюдений")
    for row in rows:
        if not isinstance(row, dict) or "mode" in row or row.get("source") == "yandex_search":
            raise ValueError("Режим задаётся оператором. Данные Yandex Search принимает адаптер v1.")
        row["mode"] = mode
    return memory.write_observations(rows)


def acquire(kind, key, *, now=None, daily_limit=None):
    instant = now or datetime.now(timezone.utc)
    started = instant.isoformat()
    lease = (instant + timedelta(minutes=5)).isoformat()
    retry_before = (instant - timedelta(minutes=5)).isoformat()
    with ai_store.database() as db:
        if daily_limit is not None:
            today = instant.date().isoformat()
            requested = db.execute("""SELECT COALESCE(sum(attempts),0) FROM memory_sync_jobs
                                       WHERE kind=? AND started>=?""", (kind, today)).fetchone()[0]
            if requested >= daily_limit:
                return None
        cursor = db.execute("""INSERT INTO memory_sync_jobs(kind,job_key,status,started,lease)
                         VALUES (?,?,'running',?,?)
                         ON CONFLICT(kind,job_key) DO UPDATE SET
                           status='running',started=excluded.started,lease=excluded.lease,
                           attempts=memory_sync_jobs.attempts+1,finished=NULL,detail=NULL
                         WHERE memory_sync_jobs.attempts<3 AND (
                           (memory_sync_jobs.status='running' AND memory_sync_jobs.lease<=excluded.started)
                           OR (memory_sync_jobs.status='error' AND memory_sync_jobs.finished<=?)
                         )""", (kind, key, started, lease, retry_before))
        if not cursor.rowcount:
            return None
        row = db.execute("SELECT id,attempts FROM memory_sync_jobs WHERE kind=? AND job_key=?", (kind, key)).fetchone()
        return row["id"], row["attempts"]


def settle(job, status, detail=None):
    with ai_store.database() as db:
        db.execute("""UPDATE memory_sync_jobs SET status=?, detail=?, finished=?,lease=NULL
                      WHERE id=? AND attempts=? AND status='running'""",
                   (status, detail, ai_store.now(), *job))


def sync(kind, day_key, *, manual=False):
    from .service import metrika, overview

    if kind not in {"sales", "metrika"}:
        raise ValueError("Синхронизируются только продажи и Метрика")
    if kind == "sales":
        paths = [os.getenv(key) for key in ("PM_WORKBOOK", "PM_SNAPSHOT") if os.getenv(key)]
        if not paths and not manual:
            return {"status": "disabled"}
        mode = "local" if paths else "synthetic"
        from pathlib import Path
        stat = Path(paths[0]).stat() if paths else (ROOT / "data/demo-overview.json").stat()
        identity = memory.digest([mode, stat.st_mtime_ns, stat.st_size,
                                  os.getenv("PM_SALES_SOURCE_ID"), os.getenv("PM_SALES_METHOD")])
        key = f"{day_key}:{identity}"
    else:
        selected = date.fromisoformat(memory.iso(day_key))
        if not 1 <= (date.today() - selected).days <= 1096:
            raise ValueError("Запрашивайте завершённый день в пределах 1096 дней")
        identity = hashlib.sha256(os.getenv("METRIKA_COUNTER_ID", "").encode()).hexdigest()[:20]
        key = f"{day_key}:{identity}"
        if not os.getenv("METRIKA_TOKEN") or not os.getenv("METRIKA_COUNTER_ID"):
            return {"status": "disabled"}
    limit = None
    if kind == "metrika":
        limit = int(os.getenv("PM_METRIKA_DAILY_LIMIT", "4"))
        if not 1 <= limit <= 24:
            raise ValueError("Дневной лимит Метрики должен быть от 1 до 24")
    job = acquire(kind, key, daily_limit=limit)
    if job is None:
        return {"status": "already_scheduled"}
    try:
        if kind == "sales":
            result = capture_sales(overview(for_analysis=True), mode)
            detail = f"created:{result['created']}"
        else:
            result = metrika(day_key, day_key, detailed=True)
            if result["status"] not in {"ok", "partial"}:
                settle(job, "error", result.get("reason", "source_unavailable"))
                return {"status": "error"}
            detail = result["status"]
        settle(job, "ok", detail)
        return {"status": "ok", "detail": detail}
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
        settle(job, "error", "source_or_storage_error")
        return {"status": "error"}


def start():
    seconds = interval()
    stop = threading.Event()

    def loop():
        while not stop.is_set():
            yesterday = (date.today() - timedelta(days=1)).isoformat()
            try:
                sync("sales", yesterday)
                if os.getenv("PM_COLLECT_METRIKA", "").lower() == "true" and not stop.is_set():
                    sync("metrika", yesterday)
            except (OSError, sqlite3.Error, ValueError):
                pass
            stop.wait(seconds)

    if seconds:
        worker = threading.Thread(target=loop, name="product-memory", daemon=True)
        worker.start()
        return stop, worker
    return stop, None
