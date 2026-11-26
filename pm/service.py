import json
import os
from pathlib import Path
from datetime import date, datetime, timezone
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .analytics import WEEK, compare_series, day, finite, reach_summary, weeks
from .settings import ROOT, workbook_case


def overview():
    from .analytics import compile_book

    if os.getenv("PM_WORKBOOK"):
        return compile_book(os.environ["PM_WORKBOOK"])
    external = os.getenv("PM_SNAPSHOT")
    path = Path(external) if external else ROOT / "data/demo-overview.json"
    snapshot = json.loads(path.read_text(encoding="utf-8-sig"))
    if external and os.getenv("PM_CASE_CONFIG"):
        snapshot.setdefault("release_id", workbook_case()["release_id"])
    for key, value in reach_summary(snapshot["reach"]).items():
        snapshot.setdefault(key, value)
    return recalculate(snapshot, {})


def recalculate(snapshot, values):
    cfg = snapshot["config"].copy()
    keys = (
        "before_start",
        "before_end",
        "after_start",
        "after_end",
        "metric",
        "alert_percent",
    )
    cfg.update({k: values[k] for k in keys if k in values})
    a, b, c, d = (day(cfg[k]) for k in keys[:4])
    base, post = weeks(a, b), weeks(c, d)
    launch = day(snapshot["launch"])
    partial = day(cfg["partial_week"])
    extended = weeks(c, partial)
    if b + WEEK > launch or c <= launch or d >= partial:
        raise ValueError("В основное окно попали неделя запуска или неполная неделя")
    if (
        cfg["metric"] not in ("orders_index", "orders_index_4w")
        or finite(cfg["alert_percent"]) > 100
    ):
        raise ValueError("Проверьте метрику и порог")
    summaries = [
        compare_series(series["points"], base, post, extended, cfg["metric"])
        for series in snapshot["series"]
    ]
    snapshot["config"].update(cfg)
    for series, summary in zip(snapshot["series"], summaries):
        series.update(summary)
    return snapshot


def metrika(start, end, opener=urlopen):
    a, b = day(start), day(end)
    if not 0 <= (b - a).days <= 92 or b > date.today():
        raise ValueError("Укажите прошедший период до 93 дней")
    counter, token = os.getenv("METRIKA_COUNTER_ID", ""), os.getenv("METRIKA_TOKEN", "")
    if not counter.isdigit() or not token:
        return {
            "available": False,
            "message": "Для запроса нужны серверные METRIKA_COUNTER_ID и METRIKA_TOKEN.",
        }
    args = urlencode(
        dict(
            ids=counter, metrics="ym:s:visits", date1=start, date2=end, accuracy="full"
        )
    )
    req = Request(
        "https://api-metrika.yandex.net/stat/v1/data?" + args,
        headers={"Authorization": "OAuth " + token},
    )
    try:
        with opener(req, timeout=15) as stream:
            data = json.load(stream)
        count = finite(data["totals"][0])
    except HTTPError as e:
        return {
            "available": False,
            "message": f"API Метрики вернул HTTP {e.code}. Проверьте права, период и лимит запросов.",
        }
    except (OSError, ValueError, TypeError, KeyError, IndexError):
        return {
            "available": False,
            "message": "Ответ API недоступен или не содержит результата.",
        }
    return dict(
        available=True,
        source="Яндекс.Метрика · настроенный счётчик",
        metric="ym:s:visits",
        start=start,
        end=end,
        value=count,
        sampled=data.get("sampled"),
        retrieved=datetime.now(timezone.utc).isoformat(),
    )
