import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from scripts.deploy import estimate_cost, health

ROOT = Path(__file__).resolve().parents[1]


class OperationsTests(unittest.TestCase):
    def test_estimate_requires_all_rates_and_counts_shared_model_once(self):
        data = estimate_cost.template()
        data.update(region="synthetic", quote_date="2026-01-01")
        with self.assertRaises((ValueError, ArithmeticError)):
            estimate_cost.estimate(data)
        for item in data["items"].values():
            item.update(quantity=0, rub_per_unit=0)
        data["items"]["application_vm"].update(quantity="2.5", rub_per_unit="1.20")
        result = estimate_cost.estimate(data)
        self.assertEqual(result["monthly_rub"], "3.00")
        self.assertEqual(result["rub_per_manager"], "0.60")

    def test_combined_quote_keeps_fixed_host_and_measured_load_separate(self):
        from scripts.qwen.cost import PER_MANAGER

        quote = estimate_cost.template()
        quote.update(region="synthetic", quote_date="2026-01-01")
        for item in quote["items"].values():
            item.update(quantity=0, rub_per_unit=0)
        quote["items"]["application_vm"].update(quantity=720, rub_per_unit=2)
        quote["items"]["dedicated_model_vm"].update(quantity=720, rub_per_unit=3)
        quote["items"]["smtp"].update(quantity=1, rub_per_unit="0.45")
        benchmark = {"mode": "live", "samples": [
            {"id": name, "wall_seconds": 10} for name in (*PER_MANAGER, "monitor")
        ]}
        result = estimate_cost.with_qwen(quote, benchmark)
        self.assertEqual(result["fixed_and_usage_quote_month"]["monthly_rub"], "3600.45")
        rows = result["model_compute_estimate_week"]["estimates"]
        self.assertEqual([row["managers"] for row in rows], [1, 5, 35])
        self.assertEqual(result["model_compute_estimate_week"]["machine_hour_price"], 3)
        self.assertEqual(rows[0]["weekly_dedicated_host_estimate"], 504)
        self.assertLess(rows[2]["weekly_compute_estimate"], rows[2]["weekly_dedicated_host_estimate"])

    def test_diagnostics_do_not_disclose_private_values_or_create_db(self):
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        path = folder / "ai.sqlite3"
        self.assertFalse(health.storage(path)["ok"])
        self.assertFalse(path.exists())
        with closing(sqlite3.connect(path)) as db:
            db.executescript("""
                CREATE TABLE memory_sync_jobs(kind,status,finished);
                CREATE TABLE runs(created,report);
                INSERT INTO memory_sync_jobs VALUES('sales','ok','not-a-date-private');
                INSERT INTO runs VALUES('now','{"secret":"private","sources_status":{"metrika":{"status":"error"}}}');
                INSERT INTO runs VALUES('2026-01-01T20:00:00+00:00',
                                        '{"sources_status":{"competitor_web":{"status":"ok"}}}');
            """)
        stats = health.storage(path)
        self.assertTrue(stats["ok"])
        self.assertEqual(stats["sources"]["metrika"]["degraded_in_last_20_runs"], 1)
        self.assertEqual(stats["sources"]["competitor_web"]["last_successful_run"], "2026-01-01T20:00:00+00:00")
        self.assertNotIn("private", json.dumps(stats))
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        status = folder / "status.json"
        record = dict(status="ok", last_backup=stamp, last_integrity_check=stamp,
                      last_restore_check=stamp, untrusted="private")
        status.write_text(json.dumps(record))
        self.assertTrue(health.backup_status(status, 36)["ok"])
        self.assertNotIn("private", json.dumps(health.backup_status(status, 36)))
        record["last_restore_check"] = "2020-01-01T00:00:00+00:00"
        status.write_text(json.dumps(record))
        self.assertFalse(health.backup_status(status, 36)["ok"])

    def test_scanner_checks_staged_contents_without_echoing_credentials(self):
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        subprocess.run(["git", "init", "-q"], cwd=folder, check=True)
        fixture = "y0_" + "a" * 40
        file = folder / "config.txt"
        file.write_text(fixture)
        subprocess.run(["git", "add", "config.txt"], cwd=folder, check=True)
        file.write_text("benign")
        scan = subprocess.run([sys.executable, str(ROOT / "scripts/deploy/scan_index.py")],
                              cwd=folder, capture_output=True, text=True)
        self.assertEqual(scan.returncode, 1)
        self.assertIn("config.txt", scan.stderr)
        self.assertNotIn(fixture, scan.stderr + scan.stdout)

    def test_scanner_checks_credentials_removed_after_an_unpublished_commit(self):
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))

        def git(*args):
            return subprocess.check_output(["git", "-c", "user.name=Test",
                                            "-c", "user.email=test@example.com", *args],
                                           cwd=folder).decode().strip()

        git("init", "-q")
        git("commit", "--quiet", "--allow-empty", "-m", "Base")
        base = git("rev-parse", "HEAD")
        fixture = "ghp_" + "a" * 36
        file = folder / "settings.txt"
        file.write_text(fixture)
        git("add", "settings.txt")
        git("commit", "--quiet", "-m", "Synthetic credential")
        git("rm", "--quiet", "settings.txt")
        git("commit", "--quiet", "-m", "Remove fixture")
        check = [sys.executable, str(ROOT / "scripts/deploy/scan_index.py")]
        subprocess.run(check, cwd=folder, capture_output=True, check=True)
        scan = subprocess.run([*check, "--since", base], cwd=folder, capture_output=True, text=True)
        self.assertEqual(scan.returncode, 1)
        self.assertIn("settings.txt", scan.stderr)
        self.assertNotIn(fixture, scan.stderr + scan.stdout)
