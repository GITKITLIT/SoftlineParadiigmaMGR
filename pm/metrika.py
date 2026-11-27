"""Server-side reporting on a configured Yandex Metrika counter."""
import hashlib
import json
import os
from collections import OrderedDict
from datetime import date, datetime, timezone
from threading import Lock
from time import monotonic, sleep
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, build_opener

from .analytics import day, finite
from .qwen import NoRedirect

METRICS = {
    "visits": ("ym:s:visits", "visits"),
    "users": ("ym:s:users", "users"),
    "pageviews": ("ym:s:pageviews", "views"),
    "bounceRate": ("ym:s:bounceRate", "percent"),
    "avgVisitDurationSeconds": ("ym:s:avgVisitDurationSeconds", "seconds"),
}
CACHE = OrderedDict()
LOCK = Lock()
OPEN = build_opener(NoRedirect()).open


def configuration(name):
    try:
        value = json.loads(os.getenv(name, "{}"))
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def metrika(start, end, opener=None, *, detailed=False, segment=None):
    a, b = day(start), day(end)
    if not 0 <= (b - a).days <= 92 or b > date.today():
        raise ValueError("Укажите прошедший период до 93 дней")
    start, end = a.isoformat(), b.isoformat()
    scope = "demo_counter" if os.getenv("METRIKA_SCOPE") != "store_counter" else "store_counter"
    period = {"start": start, "end": end}
    base = dict(available=False, period=period, scope=scope, segment=segment, metrics={})
    counter, token = os.getenv("METRIKA_COUNTER_ID", ""), os.getenv("METRIKA_TOKEN", "")
    if not counter.isascii() or not counter.isdigit() or not token:
        return base | dict(status="missing", reason="not_configured",
                           message="Для запроса нужны серверные METRIKA_COUNTER_ID и METRIKA_TOKEN.")
    segments = configuration("METRIKA_SEGMENTS")
    if segment is not None and (
        not isinstance(segment, str) or not isinstance(segments.get(segment), str)
        or not segments[segment] or len(segments[segment]) > 1000
    ):
        raise ValueError("Неизвестный сегмент Метрики")
    metrics = dict(METRICS) if detailed else {"visits": METRICS["visits"]}
    if detailed:
        for name, goal in list(configuration("METRIKA_GOALS").items())[:5]:
            if type(goal) is int and goal > 0 and name.isidentifier() and len(name) <= 40:
                metrics["goal_" + name] = (f"ym:s:goal{goal}reaches", "completions")
    query = dict(ids=counter, metrics=",".join(v[0] for v in metrics.values()),
                 date1=start, date2=end, accuracy="full")
    if segment:
        query["filters"] = segments[segment]
    args = urlencode(query)
    key = hashlib.sha256((args + "|" + token + "|" + scope).encode()).digest()
    if opener is None:
        with LOCK:
            cached = CACHE.get(key)
            if cached and monotonic() - cached[0] < 120:
                return cached[1] | {"cached": True}
    req = Request("https://api-metrika.yandex.net/stat/v1/data?" + args,
                  headers={"Authorization": "OAuth " + token})
    for attempt in range(2 if opener is None else 1):
        try:
            with (opener or OPEN)(req, timeout=6) as stream:
                data = json.load(stream)
            totals = data["totals"]
            if not isinstance(totals, list) or len(totals) != len(metrics):
                raise ValueError
            values = {
                name: dict(metric=spec[0] if not name.startswith("goal_") else name,
                           unit=spec[1], value=finite(value) if value is not None else None)
                for (name, spec), value in zip(metrics.items(), totals)
            }
            status = "ok" if all(v is not None for v in totals) else "partial"
            result = base | dict(available=values["visits"]["value"] is not None, status=status,
                                 source="Яндекс.Метрика · настроенный счётчик", metric="ym:s:visits",
                                 start=start, end=end, value=values["visits"]["value"],
                                 sampled=data.get("sampled") is True, metrics=values, cached=False,
                                 missing=["ecommerce"] + ([] if len(metrics) > len(METRICS) else ["goal"]),
                                 retrieved=datetime.now(timezone.utc).isoformat())
            if not result["available"]:
                result["message"] = "Метрика не вернула число визитов."
            if opener is None:
                import sqlite3
                from .collection import save_meter
                try:
                    save_meter(result, counter, segments.get(segment), definitions=metrics, coverage=scope)
                except (OSError, sqlite3.Error, ValueError):
                    result["memory_status"] = "write_failed"
                with LOCK:
                    CACHE[key] = (monotonic(), result)
                    while len(CACHE) > 64:
                        CACHE.popitem(last=False)
            return result
        except HTTPError as error:
            code = error.code
            error.close()
            if attempt == 0 and opener is None and (code == 429 or 500 <= code < 600):
                sleep(0.3)
                continue
            return base | dict(status="unavailable", reason=f"http_{code}",
                               message=f"API Метрики вернул HTTP {code}. Проверьте права, период и лимит запросов.")
        except (OSError, ValueError, TypeError, KeyError, IndexError):
            return base | dict(status="unavailable", reason="invalid_or_unavailable",
                               message="Ответ API недоступен или не содержит результата.")
    raise AssertionError("Unreachable retry loop")
