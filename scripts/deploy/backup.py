"""Publish a new, restore-tested snapshot using AI Core's backup implementation."""
import argparse
import fcntl
import json
import os
import re
import shutil
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pim_backup as core


INPUT = Path("/run/paradiigma_backup_input")
VAULT = Path("/var/backups/paradiigma")
STATUS = Path("/var/lib/paradiigma-backup-status/status.json")
SNAPSHOT = re.compile(r"pim-(\d{8}T\d{6}Z)-[0-9a-f]{12}")


def verify_mount():
    """Refuse a plain directory or a bind mount with weaker VFS flags."""
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        fields = line.split()
        if fields[4] == str(INPUT) and {"ro", "nosymfollow"} <= set(fields[5].split(",")):
            return
    raise ValueError("Backup input mount requires ro,nosymfollow")


def prune(vault, newest, days):
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    for item in vault.iterdir():
        match = SNAPSHOT.fullmatch(item.name)
        if item == newest or not match or item.is_symlink() or not item.is_dir():
            continue
        stamp = datetime.strptime(match[1], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        if stamp < cutoff:
            shutil.rmtree(item)


def sync_directory(directory):
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def run(database, vault, status, days):
    if not 1 <= days <= 3650:
        raise ValueError("Retention must be 1..3650 days")
    database = core.external_input(database)
    if not database.is_file():
        raise ValueError("Backup source is unavailable")
    # Serialize capture, publication and retention for timer and operator runs.
    with (vault / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        # A SIGKILL can leave an unpublished private staging tree. Reclaim only
        # our working directory under lock, preserving every published snapshot.
        work = vault / ".work"
        if work.exists():
            shutil.rmtree(work)
        work.mkdir(mode=0o700)
        try:
            legacy = database.parent / "sources.json"
            documents = [legacy] if os.path.lexists(legacy) else []
            before = core.migration_state(database)
            captured, manifest = core.create(database, work, documents)
            drill = core.restore(captured, work / "drill")
            for name in [*manifest["files"], "manifest.json"]:
                with (captured / name).open("rb") as stream:
                    os.fsync(stream.fileno())
            sync_directory(captured)
            final = vault / captured.name
            # Only this identity can create entries here, all publishers hold the lock.
            if os.path.lexists(final):
                raise FileExistsError("Snapshot identifier collision")
            captured.rename(final)
            sync_directory(vault)
            prune(vault, final, days)
            core.write_status(status, {
                "status": "ok", "last_backup": manifest["created"],
                "last_integrity_check": manifest["verified"],
                "last_restore_check": core.utc(), "restore_seconds": drill["restore_seconds"],
                "snapshot_id": final.name,
                **core.migration_fields(database, before, manifest["sqlite_version"]),
            })
            return final
        finally:
            shutil.rmtree(work)


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--retain-days", type=int, default=14)
    args = cli.parse_args()
    os.umask(0o077)
    try:
        verify_mount()
        snapshot = run(INPUT / core.DB_NAME, VAULT, STATUS, args.retain_days)
        print(json.dumps({"status": "ok", "snapshot": snapshot.name}))
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error) as exc:
        try:
            core.write_status(STATUS, {"status": "error", "last_failure": core.utc()})
        except (ValueError, OSError):
            pass
        print(json.dumps({"status": "error", "reason": type(exc).__name__}), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
