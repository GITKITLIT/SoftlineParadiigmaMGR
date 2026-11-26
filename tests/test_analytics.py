import unittest
from copy import deepcopy
from datetime import date, timedelta
from unittest.mock import Mock, patch

from pm.analytics import compile_book, reach_summary
from pm.service import recalculate


def snapshot():
    start = date(2024, 1, 1)
    points = [
        [(start + timedelta(weeks=i)).isoformat(), weekly, rolling]
        for i, (weekly, rolling) in enumerate(
            [(100, 100), (100, 100), (900, 300), (80, 95), (70, 85), (30, 70)]
        )
    ]
    return {
        "launch": "2024-01-17",
        "config": {
            "before_start": points[0][0],
            "before_end": points[1][0],
            "after_start": points[3][0],
            "after_end": points[3][0],
            "partial_week": points[5][0],
            "metric": "orders_index",
            "alert_percent": 20,
        },
        "series": [
            {"points": points},
            {"points": [[point[0], 50, 50] for point in points]},
        ],
    }


class AnalyticsTests(unittest.TestCase):
    def test_windows_and_exact_threshold(self):
        data = recalculate(snapshot(), {})
        main, control = data["series"]
        self.assertEqual((main["before"], main["after"], main["rolling_base"]), (100, 80, 100))
        self.assertEqual(main["extra"], 60)
        self.assertEqual(main["delta"], -20)
        self.assertEqual(control["delta"], 0)
        recalculate(data, {"after_end": "2024-01-29", "alert_percent": 25})
        self.assertEqual(main["delta"], -25)
        self.assertEqual(data["config"]["alert_percent"], 25)
        recalculate(data, {"metric": "orders_index_4w"})
        self.assertEqual(main["after"], 90)

    def test_zero_base(self):
        data = snapshot()
        for point in data["series"][0]["points"][:2]:
            point[1] = 0
        main = recalculate(data, {})["series"][0]
        self.assertIsNone(main["delta"])
        self.assertIsNone(main["extra_delta"])

    def test_rejects_invalid_windows_without_changing_snapshot(self):
        for settings in (
            {"before_end": "2024-01-15"},
            {"after_start": "2024-01-15"},
            {"after_end": "2024-02-05"},
            {"before_start": "2024-01-01garbage"},
        ):
            with self.subTest(settings=settings):
                data = snapshot()
                original = deepcopy(data)
                with self.assertRaises(ValueError):
                    recalculate(data, settings)
                self.assertEqual(data, original)

    def test_rejects_missing_duplicate_and_non_monday_points(self):
        for kind in ("missing", "duplicate", "non_monday"):
            with self.subTest(kind=kind):
                data = snapshot()
                points = data["series"][1]["points"]
                if kind == "missing":
                    points.pop(3)
                elif kind == "duplicate":
                    points.append(points[3][:])
                else:
                    points[3][0] = "2024-01-23"
                original = deepcopy(data)
                with self.assertRaises(ValueError):
                    recalculate(data, {})
                self.assertEqual(data, original)

    def test_workbook_reach_uses_counts_and_reports_coverage(self):
        data = snapshot()
        periods = {
            key: value for key, value in data["config"].items()
            if key not in ("metric", "alert_percent")
        }
        periods.update(reach_start="2024-01-22", reach_end="2024-01-24")
        case = dict(
            periods=periods, families=["primary", "control"], channel="web",
            release_id="release", backlog_id="task", reach_product="selector",
        )
        reach_rows = [
            ("metric_id", "product", "segment", "date", "detail_json"),
            ("cfg_reach_rate", "selector", "desktop", "2024-01-22",
             '{"configurator_visits": 9, "family_visits": 10}'),
            ("cfg_reach_rate", "selector", "desktop", "2024-01-23", None),
            ("cfg_reach_rate", "selector", "mobile", "2024-01-22",
             '{"configurator_visits": 1, "family_visits": 90}'),
        ]
        sheets = {
            "Продажи": [
                ("week_start", "product_family", "channel", "orders_index", "orders_index_4w"),
                *((week, family, "web", weekly, rolling)
                  for family, series in zip(case["families"], data["series"])
                  for week, weekly, rolling in series["points"]),
            ],
            "Релизы": [("release_id", "release_date", "related_task_ids"),
                        ("release", data["launch"], "task")],
            "Бэклог_задачи": [("task_id", "title", "status"), ("task", "Check", "Open")],
            "Метрики_данные": reach_rows,
        }
        book = Mock()
        book.__getitem__ = Mock(side_effect=lambda key: Mock(values=sheets[key]))
        with patch("pm.analytics.workbook_case", return_value=case), patch(
            "pm.analytics.load_workbook", return_value=book
        ):
            result = compile_book("synthetic.xlsx")
            self.assertEqual(result["reach_rate"], 0.1)
            self.assertEqual(result["cart_event"], {"available": False})
            self.assertEqual(result["reach"]["desktop"], dict(num=9, den=10, days=1, total=3))
            reach_rows.append(reach_rows[2])
            with self.assertRaises(ValueError):
                compile_book("synthetic.xlsx")
        self.assertIsNone(reach_summary({"desktop": {"num": 0, "den": 0}})["reach_rate"])
