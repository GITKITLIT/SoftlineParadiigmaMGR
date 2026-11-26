"""In-process periodic comparison. All scheduled work stays local."""
import hashlib
import os
import threading

from . import ai_store
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


def check(config):
    if not isinstance(config, dict):
        raise ValueError("Настройки сравнения должны быть объектом")
    if not ACTIVE.acquire(blocking=False):
        return {"status": "busy", "notification": False}
    try:
        data = recalculate(overview(), config)
        core = data["series"][0]
        delta = core["delta"]
        cfg = data["config"]
        signal = delta is not None and delta < 0 and -delta >= cfg["alert_percent"]
        status = "missing" if delta is None else "signal" if signal else "normal"
        signature = hashlib.sha256(ai_store.dumps({
            "release": data.get("release_id"), "name": core["name"], "config": cfg,
        }).encode()).hexdigest()
        return ai_store.record_check(signature, status, delta)
    finally:
        ACTIVE.release()


def start(seconds):
    stop = threading.Event()
    if not seconds:
        return stop, None

    def loop():
        while not stop.wait(seconds):
            try:
                check({})
            except (OSError, ValueError, KeyError, TypeError, IndexError):
                ai_store.record_check("scheduled", "error", None)

    worker = threading.Thread(target=loop, name="local-monitor", daemon=True)
    worker.start()
    return stop, worker
