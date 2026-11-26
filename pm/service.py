import json
import os
from pathlib import Path
from statistics import mean

from .analytics import WEEK, change, day, finite, weeks
from .metrika import metrika
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
    return snapshot


def recalculate(snapshot, values, *, allow_partial=False):
    cfg = snapshot["config"]
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
    if b + WEEK > launch or c <= launch or d >= day(cfg["partial_week"]):
        raise ValueError("В основное окно попали неделя запуска или неполная неделя")
    idx = {"orders_index": 1, "orders_index_4w": 2}.get(cfg["metric"])
    if idx is None or not 0 <= finite(cfg["alert_percent"]) <= 100:
        raise ValueError("Проверьте метрику и порог")
    for s in snapshot["series"]:
        points = {day(v[0]): v for v in s["points"]}

        def avg(ds, col):
            if any(w not in points or points[w][col] is None for w in ds):
                if allow_partial:
                    return None
                raise ValueError("Выбранное окно содержит пропуски")
            return mean(finite(points[w][col]) for w in ds)

        before, after = avg(base, idx), avg(post, idx)
        ext = avg(weeks(c, day(cfg["partial_week"])), idx)
        s.update(
            before=before,
            after=after,
            extra=ext,
            delta=change(before, after) if after is not None else None,
            extra_delta=change(before, ext) if ext is not None else None,
            rolling_base=avg(base, 2),
        )
    return snapshot
