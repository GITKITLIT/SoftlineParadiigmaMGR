"""Independent SQLite snapshot service and administrator-only restore."""
import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
DB_NAME = "ai.sqlite3"
DOCUMENT_MIME = {".txt", ".xlsx", ".xlsm", ".pdf", ".pptx", ".docx", ".json"}


def external(path):
    path = Path(path).resolve()
    if path.is_relative_to(ROOT):
        raise ValueError("Рабочая БД, копии и восстановление должны быть вне репозитория")
    return path


def checksum(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def check_database(path):
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        verdict = db.execute("PRAGMA integrity_check").fetchall()
        if verdict != [("ok",)] or db.execute("PRAGMA foreign_key_check").fetchone():
            raise ValueError("SQLite не прошла проверку целостности")
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version < 1 or not db.execute("SELECT 1 FROM sqlite_master WHERE name='runs'").fetchone():
            raise ValueError("Это не продуктовая база данных")
        return version


def utc():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_status(path, fields):
    if path is None:
        return
    path = external(path)
    old = {}
    if path.is_file() and path.stat().st_size < 4000:
        old = json.loads(path.read_text(encoding="utf8"))
    tmp = path.with_name(path.name + ".pending-" + uuid4().hex)
    try:
        with tmp.open("x", encoding="utf8") as out:
            json.dump(old | fields, out)
            out.flush()
            os.fsync(out.fileno())
        if os.name == "posix":
            os.chmod(tmp, 0o640)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def create(database, directory, documents=(), status=None):
    source, directory = external(database), external(directory)
    if not source.is_file() or directory == source.parent or directory.is_relative_to(source.parent):
        raise ValueError("Для копии нужен отдельный каталог вне состояния приложения")
    started = utc()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = directory / ("pim-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:12])
    target.mkdir(mode=0o700)
    copied = target / DB_NAME
    clock = time.monotonic()
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=5)) as incoming:
        with closing(sqlite3.connect(copied)) as outgoing:
            def budget(status, remaining, total):
                if time.monotonic() - clock > 100:
                    raise TimeoutError("SQLite snapshot exceeded 100 seconds")
            incoming.backup(outgoing, pages=128, progress=budget, sleep=0.05)
    version = check_database(copied)
    files = {DB_NAME: checksum(copied)}
    for document in documents:
        document = external(document)
        if (not document.is_file() or document.suffix.casefold() not in DOCUMENT_MIME
                or document.stat().st_size > 100_000_000 or document.name in {DB_NAME, "manifest.json"}):
            raise ValueError("Недопустимый файл приложения к копии")
        if document.name in files:
            raise ValueError("Повторяется имя приложения")
        with document.open("rb") as stream, (target / document.name).open("xb") as destination:
            shutil.copyfileobj(stream, destination)
        files[document.name] = checksum(target / document.name)
    manifest = {"schema": 1, "created": started, "verified": utc(), "sqlite_version": version, "files": files}
    with (target / "manifest.json").open("x", encoding="utf8") as output:
        json.dump(manifest, output, indent=2)
        output.flush()
        os.fsync(output.fileno())
    write_status(status, {"status": "ok", "last_backup": started, "last_integrity_check": manifest["verified"]})
    return target, manifest


def verify(directory):
    directory = external(directory)
    manifest_path = directory / "manifest.json"
    if manifest_path.is_symlink() or manifest_path.stat().st_size > 40_000:
        raise ValueError("Недопустимый манифест")
    manifest = json.loads(manifest_path.read_text(encoding="utf8"))
    files = manifest.get("files", {})
    if manifest.get("schema") != 1 or DB_NAME not in files:
        raise ValueError("Отсутствует база в манифесте")
    for name, expected in files.items():
        path = directory / name
        if (Path(name).name != name or name in {".", ".."} or path.is_symlink() or not path.is_file()
                or checksum(path) != expected):
            raise ValueError("Контрольная сумма или имя файла не прошли проверку")
    if check_database(directory / DB_NAME) != manifest["sqlite_version"]:
        raise ValueError("Версия схемы отличается от манифеста")
    return manifest


def restore(directory, destination, status=None):
    started = time.monotonic()
    source, target = external(directory), external(destination)
    if target.is_relative_to(source) or source.is_relative_to(target):
        raise ValueError("Пути копии и восстановления должны быть независимы")
    manifest = verify(source)
    target.mkdir(mode=0o700, parents=False, exist_ok=False)
    # Verify during recovery again, before any caller opens the restored database.
    for name, expected in manifest["files"].items():
        with (source / name).open("rb") as incoming, (target / name).open("xb") as outgoing:
            shutil.copyfileobj(incoming, outgoing)
        if checksum(target / name) != expected:
            raise ValueError("Копия изменилась во время восстановления")
    check_database(target / DB_NAME)
    elapsed = time.monotonic() - started
    write_status(status, {"last_restore_check": utc(), "restore_seconds": round(elapsed, 3)})
    return {"status": "ok", "restore_seconds": round(elapsed, 3), "snapshot_at": manifest["created"]}


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    create_args = actions.add_parser("create")
    create_args.add_argument("--db", required=True)
    create_args.add_argument("--directory", required=True)
    create_args.add_argument("--document", action="append", default=[])
    create_args.add_argument("--status-file")
    verify_args = actions.add_parser("verify")
    verify_args.add_argument("--snapshot", required=True)
    restore_args = actions.add_parser("restore-test")
    restore_args.add_argument("--snapshot", required=True)
    restore_args.add_argument("--output", required=True)
    restore_args.add_argument("--status-file")
    args = parser.parse_args()
    try:
        if args.action == "create":
            path, result = create(args.db, args.directory, args.document, args.status_file)
            result = {"snapshot": path.name, "created": result["created"], "status": "ok"}
        elif args.action == "verify":
            result = {"status": "ok", "schema": verify(args.snapshot)["sqlite_version"]}
        else:
            result = restore(args.snapshot, args.output, args.status_file)
        print(json.dumps(result))
        return 0
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error) as error:
        # Logs and the read-only status surface never include paths or credentials.
        if args.action == "create" and args.status_file:
            try:
                write_status(args.status_file, {"status": "error", "last_failure": utc()})
            except (OSError, ValueError):
                pass
        print(json.dumps({"status": "error", "reason": type(error).__name__}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
