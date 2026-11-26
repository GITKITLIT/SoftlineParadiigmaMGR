"""Aggregate the supplied v17 schema. Only aggregate values leave this module."""

import json
import math
from datetime import date, datetime, timedelta
from statistics import mean

from openpyxl import load_workbook

from .settings import DEMO_CASE, workbook_case
DEFAULT = dict(
    **DEMO_CASE["periods"],
    metric="orders_index",
    alert_percent=20,
)
WEEK = timedelta(weeks=1)


def day(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            parsed = date.fromisoformat(value)
            if parsed.isoformat() == value:
                return parsed
        except ValueError:
            pass
    raise ValueError("Ожидается дата в формате ГГГГ-ММ-ДД")


def finite(value):
    if type(value) not in (float, int) or not math.isfinite(value) or value < 0:
        raise ValueError("Ожидается неотрицательное число")
    return value


def records(sheet):
    it = iter(sheet.values)
    headers = next(it)
    return (dict(zip(headers, row)) for row in it if any(v is not None for v in row))


def weeks(start, end):
    if start > end or start.weekday() or end.weekday():
        raise ValueError("Укажите начала недель по понедельникам в порядке возрастания")
    return [start + WEEK * i for i in range((end - start).days // 7 + 1)]


def change(before, after):
    return (after - before) / before * 100 if before else None


def compare_series(points, base, post, extended, metric):
    column = {"orders_index": 1, "orders_index_4w": 2}[metric]
    by_date = {}
    for point in points:
        week = day(point[0])
        if week.weekday():
            raise ValueError(f"Начало недели должно быть понедельником: {week}")
        if week in by_date:
            raise ValueError(f"Дублируется неделя {week}")
        by_date[week] = point

    def average(dates, index):
        if any(d not in by_date or by_date[d][index] is None for d in dates):
            raise ValueError("Выбранное окно содержит пропуски")
        return mean(finite(by_date[d][index]) for d in dates)

    before, after = average(base, column), average(post, column)
    extra = average(extended, column)
    return dict(
        before=before,
        after=after,
        extra=extra,
        delta=change(before, after),
        extra_delta=change(before, extra),
        rolling_base=average(base, 2),
    )


def reach_summary(reach):
    num = sum(device["num"] for device in reach.values())
    den = sum(device["den"] for device in reach.values())
    return dict(reach_rate=num / den if den else None, cart_event={"available": False})


def compile_book(path, config=None, source="Локальная выгрузка"):
    case = workbook_case()
    cfg = DEFAULT | case["periods"] | (config or {})
    if set(cfg) != set(DEFAULT) or cfg["metric"] not in (
        "orders_index",
        "orders_index_4w",
    ):
        raise ValueError("Неизвестная настройка или метрика")
    if finite(cfg["alert_percent"]) > 100:
        raise ValueError("Проверьте порог")
    start, end, post, full, partial, rs, re = (
        day(cfg[k])
        for k in (
            "before_start",
            "before_end",
            "after_start",
            "after_end",
            "partial_week",
            "reach_start",
            "reach_end",
        )
    )
    base, after = weeks(start, end), weeks(post, full)
    if full >= partial or partial.weekday() or re < rs:
        raise ValueError("Проверьте границы неполной недели и периода Метрики")
    extended = weeks(post, partial)
    book = load_workbook(path, read_only=True, data_only=True)
    try:
        releases = {r["release_id"]: r for r in records(book["Релизы"])}
        release = releases[case["release_id"]]
        launch = day(release["release_date"])
        if not (end + WEEK <= launch < post):
            raise ValueError(
                "Периоды сравнения включают неделю запуска или пересекаются"
            )
        tasks = {r["task_id"]: r for r in records(book["Бэклог_задачи"])}
        task = tasks[case["backlog_id"]]
        sales = list(records(book["Продажи"]))
        series = []
        for family in case["families"]:
            points = [
                [
                    day(row["week_start"]).isoformat(),
                    row["orders_index"],
                    row["orders_index_4w"],
                ]
                for row in sales
                if (row["product_family"], row["channel"]) == (family, case["channel"])
            ]
            summary = compare_series(points, base, after, extended, cfg["metric"])
            visible = sorted(point for point in points if point[0] <= partial.isoformat())
            for _, weekly, rolling in visible:
                finite(weekly)
                if rolling is not None:
                    finite(rolling)
            series.append(dict(name=family, points=visible, **summary))
        reach = {}
        for device in ("desktop", "mobile"):
            seen = set()
            num = den = covered = 0
            for r in records(book["Метрики_данные"]):
                if (r["metric_id"], r["product"], r["segment"]) != (
                    "cfg_reach_rate",
                    case["reach_product"],
                    device,
                ):
                    continue
                d = day(r["date"])
                if not rs <= d <= re:
                    continue
                if d in seen:
                    raise ValueError(f"Дублируется сегмент {device} за {d}")
                seen.add(d)
                if not r["detail_json"]:
                    continue
                z = json.loads(r["detail_json"])
                a, b = finite(z["configurator_visits"]), finite(z["family_visits"])
                if a > b:
                    raise ValueError("Числитель больше числа визитов")
                covered += 1
                num += a
                den += b
            reach[device] = dict(
                num=num, den=den, days=covered, total=(re - rs).days + 1
            )
        return dict(
            source=source,
            config=cfg,
            launch=launch.isoformat(),
            series=series,
            reach=reach,
            **reach_summary(reach),
            release_id=case["release_id"],
            release_task=release["related_task_ids"],
            backlog=dict(id=case["backlog_id"], title=task["title"], status=task["status"]),
        )
    finally:
        book.close()


def effect(rows, fix_date, target):
    """Compare complete calendar weeks around the fix for both families."""
    finite(target)
    fix = day(fix_date)
    first = fix + timedelta(days=(-fix.weekday()) % 7)
    dates = [first + i * WEEK for i in range(4)]
    by_date = {}
    for row in rows:
        d = day(row["week"])
        if d in by_date or d.weekday() or type(row.get("complete")) is not bool:
            raise ValueError(
                "Недели должны быть уникальными понедельниками с признаком полноты"
            )
        by_date[d] = row
    last_prior = fix - timedelta(days=fix.weekday()) - WEEK
    prior_dates = [last_prior - i * WEEK for i in range(4)]
    families = ("kaspersky", "astra")
    for week in prior_dates + dates:
        row = by_date.get(week)
        if (
            row is None
            or not row["complete"]
            or any(row.get(key) is None for key in families)
        ):
            return dict(status="Ожидаем данные после исправления")
    prior = {key: mean(finite(by_date[d][key]) for d in prior_dates) for key in families}
    result = {key: mean(finite(by_date[d][key]) for d in dates) for key in families}
    return dict(
        before_fix=prior,
        status="Цель достигнута"
        if result["kaspersky"] >= target
        else "Пересмотреть гипотезу",
        start=dates[0].isoformat(),
        end=(dates[-1] + timedelta(days=6)).isoformat(),
        target=target,
        **result,
    )
