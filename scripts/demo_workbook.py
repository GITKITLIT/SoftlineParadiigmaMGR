"""Create a synthetic workbook in the format accepted by the analytics module."""

import argparse
from datetime import date, timedelta
from pathlib import Path
from statistics import mean
import sys

from openpyxl import Workbook

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pm.settings import DEMO_CASE

p = argparse.ArgumentParser(description=__doc__)
p.add_argument("output", type=Path)
args = p.parse_args()
b = Workbook()
s = b.active
s.title = "Продажи"
s.append(["week_start", "product_family", "orders_index", "orders_index_4w", "channel"])
for i, family in enumerate(DEMO_CASE["families"]):
    series = []
    for n in range(12):
        week = date(2025, 1, 20) + timedelta(weeks=n)
        value = (100 if n < 7 else 70) if i == 0 else 95
        series.append(value)
        s.append(
            [
                week.isoformat(),
                family,
                value,
                mean(series[-4:]) if n >= 3 else value,
                DEMO_CASE["channel"],
            ]
        )
s = b.create_sheet("Релизы")
s.append(["release_id", "release_date", "related_task_ids"])
s.append([DEMO_CASE["release_id"], "2025-03-05", "TASK-01"])
s = b.create_sheet("Бэклог_задачи")
s.append(["task_id", "title", "status"])
s.append([DEMO_CASE["backlog_id"], "Проверить событие добавления в корзину", "Демо"])
s = b.create_sheet("Метрики_данные")
s.append(["metric_id", "product", "segment", "date", "detail_json"])
for device, visits, selection in (("desktop", 40, 12), ("mobile", 20, 5)):
    for n in range(14):
        s.append(
            [
                "cfg_reach_rate",
                DEMO_CASE["reach_product"],
                device,
                (date(2025, 3, 10) + timedelta(days=n)).isoformat(),
                f'{{"family_visits":{visits},"configurator_visits":{selection}}}',
            ]
        )
args.output.parent.mkdir(exist_ok=True, parents=True)
b.save(args.output)
