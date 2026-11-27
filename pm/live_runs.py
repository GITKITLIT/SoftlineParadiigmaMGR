"""Bounded in-process progress for browser investigations; final reports are durable."""
import sqlite3
from collections import OrderedDict
from threading import BoundedSemaphore, Lock, Thread
from time import monotonic
from uuid import uuid4

from . import investigation, qwen

JOBS = OrderedDict()
LOCK = Lock()
SLOTS = BoundedSemaphore(2)
MAX_JOBS = 48
RETENTION = 1200


def _prune():
    now = monotonic()
    for key, row in list(JOBS.items()):
        if row["status"] != "running" and (now - row["updated"] > RETENTION or len(JOBS) >= MAX_JOBS):
            del JOBS[key]


def start(request, analyze=investigation.analyze, *, private=False):
    if not SLOTS.acquire(blocking=False):
        raise ValueError("Два расследования уже выполняются. Попробуйте после завершения.")
    identifier = uuid4().hex
    with LOCK:
        _prune()
        JOBS[identifier] = {"status": "running", "phases": [], "updated": monotonic(), "private": private}

    def progress(event):
        safe = {name: event[name] for name in ("step", "tool", "actor", "status") if name in event}
        with LOCK:
            row = JOBS[identifier]
            row["phases"] = (row["phases"] + [safe])[-30:]
            row["updated"] = monotonic()

    def work():
        try:
            report = analyze(request, progress=progress)
            with LOCK:
                JOBS[identifier].update(status="ready", run_id=report["run_id"], updated=monotonic())
        except qwen.InvestigationQueueError as error:
            with LOCK:
                JOBS[identifier].update(status="failed", message=str(error), updated=monotonic())
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
            with LOCK:
                JOBS[identifier].update(status="failed", updated=monotonic())
        finally:
            with LOCK:
                if JOBS[identifier]["status"] == "running":
                    JOBS[identifier].update(status="failed", updated=monotonic())
            SLOTS.release()

    thread = Thread(target=work, name="live-investigation", daemon=True)
    try:
        thread.start()
    except RuntimeError:
        with LOCK:
            JOBS.pop(identifier, None)
        SLOTS.release()
        raise ValueError("Не удалось запустить расследование") from None
    response = {"job_id": identifier}
    if qwen.single_slot_enabled():
        # A second client may use both the queue and execution budgets.
        response["wait_seconds"] = 2 * qwen.SANDY_BRIDGE_BUDGET.total + 100
    return response


def status(identifier, *, private=False):
    if not isinstance(identifier, str) or len(identifier) != 32:
        raise ValueError("Некорректный ID запуска")
    with LOCK:
        _prune()
        row = JOBS.get(identifier)
        if not row or row["private"] != private:
            raise ValueError("Временный статус запуска больше недоступен")
        return {name: value for name, value in row.items() if name not in {"updated", "private"}}
