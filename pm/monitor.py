"""In-process periodic comparison. Investigations run locally."""
import hashlib
import os
import sqlite3
import threading

from . import ai_store, email_notifications, investigation
from .analytics import day, weeks
from .service import overview, recalculate

ACTIVE = threading.Lock()


def interval():
    raw = os.getenv("PM_MONITOR_INTERVAL", "").strip()
    if not raw:
        return 0
    seconds = int(raw)
    if not 60 <= seconds <= 86400:
        raise ValueError("Интервал мониторинга должен быть от 60 до 86400 секунд")
    return seconds


def max_daily():
    cap = int(os.getenv("PM_MONITOR_DAILY_LIMIT", "4"))
    if not 1 <= cap <= 20:
        raise ValueError("Лимит расследований должен быть от 1 до 20 в сутки")
    return cap


def check(config, *, proactive=False, analyze=investigation.analyze):
    if not isinstance(config, dict):
        raise ValueError("Настройки сравнения должны быть объектом")
    if not ACTIVE.acquire(blocking=False):
        return {"status": "busy", "notification": False}
    try:
        data = recalculate(overview(for_analysis=True), config, allow_partial=True)
        core = next(iter(data["series"]), None)
        delta = core["delta"] if core else None
        cfg = data["config"]
        complete = all(len(weeks(day(cfg[f"{key}_start"]), day(cfg[f"{key}_end"]))) >= 2
                       for key in ("before", "after"))
        usable = delta is not None and complete
        signal = usable and delta < 0 and -delta >= cfg["alert_percent"]
        status = "missing" if not usable else "signal" if signal else "normal"
        signature = hashlib.sha256(ai_store.dumps({
            "release": data.get("release_id"), "name": core["name"] if core else None,
            "mode": "local" if os.getenv("PM_WORKBOOK") or os.getenv("PM_SNAPSHOT") else "synthetic",
            "config": cfg, "scheduled": proactive,
        }).encode()).hexdigest()
        result = ai_store.record_check(signature, status, delta, enqueue=proactive)
        if proactive and signal:
            notification = ai_store.claim_investigation(signature, max_daily())
            if notification:
                run_id = None
                try:
                    report = analyze({
                        "query": "Почему упал индекс заказов? Сопоставь контроль, воронку, релизы, исследования, конкурентов и бэклог.",
                        "config": cfg,
                    })
                    run_id = report["run_id"]
                    result.update(run_id=run_id, notification_id=notification)
                finally:
                    ai_store.complete_investigation(notification, run_id)
        return result
    finally:
        ACTIVE.release()
        if proactive:
            try:
                email_notifications.deliver_pending()
            except (OSError, sqlite3.Error):
                # A claimed delivery remains leased and expires as uncertain.
                pass


def start(seconds):
    stop = threading.Event()
    if not seconds:
        return stop, None

    max_daily()

    def loop():
        while not stop.wait(seconds):
            try:
                check({}, proactive=True)
            except (OSError, ValueError, KeyError, TypeError, IndexError, sqlite3.Error):
                try:
                    ai_store.record_check("scheduled", "error", None)
                except (OSError, sqlite3.Error):
                    continue

    worker = threading.Thread(target=loop, name="local-monitor", daemon=True)
    worker.start()
    return stop, worker
