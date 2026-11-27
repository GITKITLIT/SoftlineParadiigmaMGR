import json
import os
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pm.yandex_search.snapshots import History, estimate_cost, history_scope
from scripts.search_monitor import capture, empty_result, markdown
from scripts.search_smoke import OFFLINE_INPUT, ROOT
from test_yandex_comparison import OWN
from test_yandex_market import study


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.private = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.history = History(self.private)
        self.start = datetime(2026, 11, 20, 5, tzinfo=UTC)

    def capture(self, now, rows, **kwargs):
        return capture(
            history=self.history, plan=OFFLINE_INPUT, mode="offline",
            provider=empty_result, local=rows, own=OWN, now=now, as_of=now.date(), **kwargs,
        )

    def test_history_updates_with_material_comparable_changes_only(self):
        old = study("old", "Демо A", price="1000", self_checkout="Да", support="Да",
                    study_date="2026-11-19")
        new = study("new", "Демо A", price="1100", self_checkout="Нет", support="не найдено",
                    study_date="2026-11-20")
        baseline = self.capture(self.start, [old])
        current = self.capture(self.start + timedelta(days=1), [new])
        tracker = current["changes"]
        self.assertEqual(tracker["baseline"], baseline["snapshot"]["id"])
        self.assertEqual(tracker["material_changes"], 2)
        prices = next(change for change in tracker["changes"] if change["feature"] == "price")
        self.assertEqual(prices["price_delta"], {"amount": "100", "currency": "RUB", "percent": "10.00"})
        self.assertEqual(prices["before_sources"][0]["id"], old["id"])
        self.assertEqual(prices["after_sources"][0]["id"], new["id"])
        support = next(change for change in tracker["changes"] if change["feature"] == "support")
        self.assertEqual(support["reason"], "needs_review")
        self.assertFalse(support["material"])
        scope = history_scope(OFFLINE_INPUT, "offline")
        self.assertEqual(History(self.private).latest(scope)["id"], current["snapshot"]["id"])
        self.assertIn("Существенных изменений 2", markdown(current))

    def test_same_or_older_dates_and_different_price_terms_are_not_changes(self):
        self.capture(self.start, [study("a", "Демо A", price="1000", study_date="2026-11-20")])
        replay = self.capture(self.start + timedelta(days=1), [
            study("b", "Демо A", price="1100", study_date="2026-11-20"),
        ])
        price = next(change for change in replay["changes"]["changes"] if change["feature"] == "price")
        self.assertEqual(price["reason"], "same_or_older_observation")
        new = self.capture(self.start + timedelta(days=2), [
            study("c", "Демо A", price="1300", devices="2", study_date="2026-11-22"),
        ])
        price = next(change for change in new["changes"]["changes"] if change["feature"] == "price")
        self.assertEqual(price["reason"], "different_offers")
        self.assertFalse(price["material"])

    def test_concurrent_due_claim_is_atomic_and_durable(self):
        scope = history_scope(OFFLINE_INPUT, "offline")
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: History(self.private).claim(scope, self.start, 3600),
                                    range(24)))
        tokens = [token for token, _ in results if token]
        self.assertEqual(len(tokens), 1)
        self.assertEqual({reason for _, reason in results}, {None, "already_running"})
        self.history.finish(scope, tokens[0])
        self.assertEqual(self.history.claim(scope, self.start + timedelta(minutes=10), 3600),
                         (None, "not_due"))
        self.assertTrue(self.history.claim(scope, self.start + timedelta(hours=1), 3600)[0])

    def test_tariff_accounts_for_night_day_and_upper_estimate(self):
        data = {"usage": {"requests": 3}, "attempts": [
            {"started_at": "2026-11-27T04:59:00Z", "outcome": "ok"},
            {"started_at": "2026-11-27T05:00:00Z", "outcome": "provider_unavailable"},
        ]}
        result = estimate_cost(data)
        self.assertEqual(result["estimated_upper_rub"], "1.342")
        self.assertEqual(result["requests_with_timestamps"], 2)

    def test_cli_writes_offline_report_and_respects_interval(self):
        report = self.private / "report.json"
        command = [sys.executable, str(ROOT / "scripts/search_monitor.py"), "--demo",
                   "--history-dir", str(self.private), "--minimum-interval-minutes", "60"]
        first = subprocess.run(command + ["--report", str(report)], capture_output=True)
        self.assertEqual(first.returncode, 0, first.stderr)
        doc = json.loads(report.read_text(encoding="utf-8"))
        self.assertIsNone(doc["snapshot"]["cost"])
        self.assertEqual(doc["snapshot"]["mode"], "demo")
        second = subprocess.run(command, capture_output=True)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(json.loads(second.stdout)["reason"], "not_due")
        self.assertTrue((self.private / "competitor-history.sqlite3").exists())

    def test_live_cli_is_gated_before_provider_or_history(self):
        plan = self.private / "plan.json"
        plan.write_text(json.dumps(OFFLINE_INPUT), encoding="utf-8")
        live = subprocess.run([sys.executable, str(ROOT / "scripts/search_monitor.py"), "--live",
                               "--plan", str(plan), "--history-dir", str(self.private)],
                              env=os.environ | {"YANDEX_SEARCH_MONITOR_ENABLED": "false"},
                              capture_output=True)
        self.assertEqual(live.returncode, 2)
        self.assertEqual(self.history.latest(history_scope(OFFLINE_INPUT, "live")), None)
