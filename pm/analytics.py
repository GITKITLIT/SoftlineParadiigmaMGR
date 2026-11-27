"""Aggregate the supplied v17 schema. Only aggregate values leave this module."""

import json
import math
from datetime import date, timedelta
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
    return date.fromisoformat(str(value)[:10])


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
    return 100 * (after / before - 1) if before else None


def compile_book(path, config=None, source="Локальная выгрузка", *, for_analysis=False):
    case = workbook_case()
    cfg = DEFAULT | case["periods"] | (config or {})
    if set(cfg) != set(DEFAULT) or cfg["metric"] not in (
        "orders_index",
        "orders_index_4w",
    ):
        raise ValueError("Неизвестная настройка или метрика")
    finite(cfg["alert_percent"])
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
            by_date = {}
            for r in sales:
                if r["product_family"] == family and r["channel"] == case["channel"]:
                    d = day(r["week_start"])
                    if d in by_date:
                        raise ValueError(f"Дублируется неделя {d} / {family}")
                    by_date[d] = r

            def average(dates, metric):
                if any(d not in by_date or by_date[d][metric] is None for d in dates):
                    if for_analysis:
                        return None
                    raise ValueError(f"Неполное окно по {family} / {metric}")
                return mean(finite(by_date[d][metric]) for d in dates)

            metric = cfg["metric"]
            before, main = average(base, metric), average(after, metric)
            extended = average(weeks(post, partial), metric)
            rolling_base = average(base, "orders_index_4w")
            points = [
                [d.isoformat(),
                 None if for_analysis and r["orders_index"] is None else finite(r["orders_index"]),
                 r["orders_index_4w"]]
                for d, r in sorted(by_date.items())
                if d <= partial
            ]
            series.append(
                dict(
                    name=family,
                    before=before,
                    after=main,
                    extra=extended,
                    delta=change(before, main) if main is not None else None,
                    extra_delta=change(before, extended) if extended is not None else None,
                    rolling_base=rolling_base,
                    points=points,
                )
            )
        reach = {}
        metric_rows = list(records(book["Метрики_данные"]))
        for device in ("desktop", "mobile"):
            seen = set()
            num = den = 0
            for r in metric_rows:
                if (r["metric_id"], r["product"], r["segment"]) != (
                    "cfg_reach_rate",
                    case["reach_product"],
                    device,
                ):
                    continue
                d = day(r["date"])
                if not rs <= d <= re or not r["detail_json"]:
                    continue
                if d in seen:
                    raise ValueError(f"Дублируется сегмент {device} за {d}")
                z = json.loads(r["detail_json"])
                a, b = finite(z["configurator_visits"]), finite(z["family_visits"])
                if a > b:
                    raise ValueError("Числитель больше числа визитов")
                seen.add(d)
                num += a
                den += b
            reach[device] = dict(
                num=num, den=den, days=len(seen), total=(re - rs).days + 1
            )
        snapshot = dict(
            source=source,
            config=cfg,
            launch=launch.isoformat(),
            series=series,
            reach=reach,
            release_id=case["release_id"],
            release_task=release["related_task_ids"],
            backlog=dict(id=case["backlog_id"], title=task["title"], status=task["status"]),
        )
        if for_analysis:
            basket = {}
            for device in ("desktop", "mobile"):
                observed = {
                    day(row["date"]) for row in metric_rows
                    if (row["metric_id"], row["product"], row["segment"]) ==
                    ("cfg_add_to_cart_rate", case["reach_product"], device)
                    and row.get("value") is not None and rs <= day(row["date"]) <= re
                }
                basket[device] = {"days": len(observed), "total": (re - rs).days + 1}
            snapshots = []
            for key, row in releases.items():
                try:
                    released = day(row.get("release_date"))
                except ValueError:
                    continue
                if start <= released <= partial and key and key != case["release_id"]:
                    snapshots.append(dict(id=str(key), date=released.isoformat(),
                                          title=str(row.get("title") or row.get("release_name") or row.get("description") or "")[:180],
                                          related_task=row.get("related_task_ids")))
            snapshots.sort(key=lambda row: (abs(day(row["date"]) - launch), row["id"]))
            snapshot.update(
                channel=case["channel"],
                release_title=str(release.get("title") or release.get("release_name") or release.get("description") or "")[:240],
                reach_label="Визиты с конфигуратором",
                basket_coverage=basket,
                release_history=snapshots[:12],
                backlog_history=[
                    {"id": str(key), "title": str(row.get("title") or "")[:180], "status": row.get("status")}
                    for key, row in tasks.items() if key and key != case["backlog_id"]
                ][:200],
            )
        return snapshot
    finally:
        book.close()


def effect(rows, fix_date, target, *, allow_missing_values=False):
    """Earliest four fully observed calendar weeks after a fix, aligned across families."""
    finite(target)
    fix = day(fix_date)
    first = fix + timedelta(days=(-fix.weekday()) % 7)
    dates = [first + i * WEEK for i in range(4)]
    by_date = {}
    for row in rows:
        d = day(row["week"])
        if d in by_date or d.weekday() or type(row["complete"]) is not bool:
            raise ValueError(
                "Недели должны быть уникальными понедельниками с признаком полноты"
            )
        by_date[d] = row
    if any(d not in by_date or not by_date[d]["complete"] for d in dates):
        return dict(status="Ожидаем данные после исправления")
    def aggregate(window, key):
        values = [by_date[d].get(key) for d in window]
        if allow_missing_values and None in values:
            return None
        return mean(finite(value) for value in values)

    result = {key: aggregate(dates, key) for key in ("kaspersky", "astra")}
    if result["kaspersky"] is None:
        return dict(status="Ожидаем данные после исправления")
    prior_dates = (
        [first - i * WEEK for i in range(1, 5)]
        if fix == first
        else [first - i * WEEK for i in range(2, 6)]
    )
    prior = None
    if all(d in by_date and by_date[d]["complete"] for d in prior_dates):
        prior = {
            key: aggregate(prior_dates, key)
            for key in ("kaspersky", "astra")
        }
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
