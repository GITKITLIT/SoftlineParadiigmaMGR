"""Local, read-only operations report. Print only allowlisted metadata."""
import argparse
import http.client
import json
import shutil
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


def probe(port, model=False):
    started = time.monotonic()
    with closing(http.client.HTTPConnection("127.0.0.1", port, timeout=4)) as client:
        try:
            if model:
                client.request("GET", "/health")
            else:
                client.request("POST", "/api/analysis/settings", b"{}", {"Content-Type": "application/json"})
            response = client.getresponse()
            payload = json.loads(response.read(8193)) if response.status == 200 else {}
            ok = isinstance(payload, dict) and (
                payload.get("status") == "ok" if model else type(payload.get("model_configured")) is bool
            )
        except (OSError, http.client.HTTPException, ValueError):
            ok = False
    return {"ok": ok, "latency_ms": round((time.monotonic() - started) * 1000)}


def host_usage(proc=Path("/proc")):
    def ticks():
        values = [int(n) for n in (proc / "stat").read_text().splitlines()[0].split()[1:9]]
        return sum(values), sum(values[3:5])

    try:
        total0, idle0 = ticks()
        time.sleep(0.2)
        total, idle = ticks()
        mem = {line.split(":")[0]: int(line.split()[1]) * 1024
               for line in (proc / "meminfo").read_text().splitlines()
               if line.startswith(("MemTotal:", "MemAvailable:"))}
        return {"cpu_busy_pct": round(100 * (1 - (idle - idle0) / (total - total0)), 1),
                "ram_used_bytes": mem["MemTotal"] - mem["MemAvailable"], "ram_total_bytes": mem["MemTotal"]}
    except (OSError, ValueError, KeyError, IndexError, ZeroDivisionError):
        return {"unavailable": True}


def timestamp(value):
    if isinstance(value, str) and len(value) <= 40:
        try:
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo:
                return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")
        except (ValueError, OverflowError):
            pass
    return None


def backup_status(path, max_age_hours):
    try:
        with path.open("rb") as stream:
            data = json.loads(stream.read(4097))
        dates = {key: timestamp(data.get(key)) for key in
                 ("last_backup", "last_integrity_check", "last_restore_check", "last_failure")}
        now = datetime.now(timezone.utc)
        def fresh(value):
            return value is not None and 0 <= (now - datetime.fromisoformat(value)).total_seconds() <= max_age_hours * 3600

        return {**dates, "ok": data.get("status") == "ok" and all(
            fresh(dates[key]) for key in ("last_backup", "last_integrity_check", "last_restore_check"))}
    except (AttributeError, ValueError, OSError):
        return {"ok": False}


def storage(path):
    # Read numeric aggregates and timestamps only. Restrict even the set of returned source names.
    sources = ("metrika", "competitor_web")
    try:
        free = shutil.disk_usage(path.parent).free
        size = path.stat().st_size
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
            db.execute("PRAGMA query_only=ON")
            sync = {}
            for name in ("sales", "metrika"):
                ok, errors = db.execute("""
                    SELECT max(CASE WHEN status='ok' THEN finished END),
                           count(CASE WHEN status='error' THEN 1 END)
                    FROM memory_sync_jobs WHERE kind=?""", (name,)).fetchone()
                sync[name] = {"last_success": timestamp(ok), "failed_jobs": errors}
            failures = {}
            for source in sources:
                failures[source] = db.execute("""
                    SELECT count(*) FROM (SELECT report FROM runs ORDER BY created DESC LIMIT 20)
                    WHERE json_extract(report, ?) IN ('unavailable','error','timeout','partial')
                    """, ("$.sources_status." + source + ".status",)).fetchone()[0]
        wal = path.with_name(path.name + "-wal")
        return {"ok": True, "disk_free_bytes": free, "sqlite_bytes": size,
                "wal_bytes": wal.stat().st_size if wal.exists() else 0,
                "sync": sync, "degraded_in_last_20_runs": failures}
    except (sqlite3.Error, OSError, ValueError):
        return {"ok": False}


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--port", type=int, default=8080)
    cli.add_argument("--qwen-port", type=int, default=18081)
    cli.add_argument("--expect-qwen", action="store_true")
    cli.add_argument("--database", type=Path, default=Path("/var/lib/paradiigma/db/ai.sqlite3"))
    cli.add_argument("--backup-status", type=Path, default=Path("/var/lib/paradiigma-backup-status/status.json"))
    cli.add_argument("--max-backup-age-hours", type=int, default=36)
    args = cli.parse_args()
    backend, model = probe(args.port), probe(args.qwen_port, model=True)
    backup = backup_status(args.backup_status, args.max_backup_age_hours)
    database = storage(args.database)
    print(json.dumps({"backend": backend, "qwen": model, "host": host_usage(),
                      "database": database, "backup": backup}, sort_keys=True))
    return 0 if backend["ok"] and database["ok"] and backup["ok"] and (model["ok"] or not args.expect_qwen) else 1


if __name__ == "__main__":
    raise SystemExit(main())
