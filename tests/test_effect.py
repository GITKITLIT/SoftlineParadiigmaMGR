import unittest
from datetime import date, timedelta

from pm.analytics import effect


def observations():
    start = date(2024, 1, 1)
    return [
        {
            "week": (start + timedelta(weeks=i)).isoformat(),
            "complete": True,
            "kaspersky": primary,
            "astra": control,
        }
        for i, (primary, control) in enumerate(
            [(60, 40)] * 4 + [(900, 900)] + [(100, 50)] * 4 + [(500, 500)]
        )
    ]


class EffectTests(unittest.TestCase):
    def test_midweek_fix_and_saved_target(self):
        rows = observations()
        result = effect(rows[::-1], "2024-01-31", 100)
        self.assertEqual(result["before_fix"], {"kaspersky": 60, "astra": 40})
        self.assertEqual((result["kaspersky"], result["astra"]), (100, 50))
        self.assertEqual((result["start"], result["end"]), ("2024-02-05", "2024-03-03"))
        self.assertEqual(result["target"], 100)
        self.assertEqual(result["status"], "Цель достигнута")
        self.assertEqual(effect(rows, "2024-01-31", 101)["status"], "Пересмотреть гипотезу")

    def test_waits_for_both_complete_windows_and_control(self):
        fix = "2024-01-31"
        for index in (2, 6):
            with self.subTest(index=index):
                rows = observations()
                rows[index]["complete"] = False
                self.assertIn("Ожидаем", effect(rows, fix, 100)["status"])
                self.assertIn("Ожидаем", effect(rows[:index] + rows[index + 1:], fix, 100)["status"])
                rows[index]["complete"] = True
                del rows[index]["astra"]
                self.assertIn("Ожидаем", effect(rows, fix, 100)["status"])

    def test_monday_fix_starts_post_window_that_day(self):
        rows = observations()
        result = effect(rows, "2024-01-29", 100)
        self.assertEqual(result["start"], "2024-01-29")
        self.assertEqual(result["before_fix"]["kaspersky"], 60)
        self.assertEqual(result["kaspersky"], 300)

    def test_rejects_invalid_dates_and_duplicate_weeks(self):
        rows = observations()
        with self.assertRaises(ValueError):
            effect(rows, "2024-01-31oops", 100)
        with self.assertRaises(ValueError):
            effect(rows + [rows[0]], "2024-01-31", 100)
        rows[0]["week"] = "2024-01-02"
        with self.assertRaises(ValueError):
            effect(rows, "2024-01-31", 100)
