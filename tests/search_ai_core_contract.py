"""Run in the isolated AI Core copy created by scripts/search_verify_ai_core.py."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import app
from pm import ai_store, history, qwen, web_search
from pm.yandex_search.snapshots import History
from scripts.search_monitor import capture
from scripts.search_smoke import OFFLINE_INPUT, ROOT, offline_fixture
from test_yandex_comparison import OWN
from test_yandex_market import study


class SearchAIContractTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.private = self.folder / "pim"
        self.enterContext(patch.dict(os.environ, {"PM_PRIVATE_DIR": str(self.private)}, clear=True))

    def test_actual_v1_adapter_supplies_bounded_competitor_evidence_and_memory(self):
        timestamp = datetime(2026, 11, 27, tzinfo=UTC)
        fixture = ROOT / "tests/fixtures/yandex/competitor-page.html"
        original_envelope = web_search._envelope
        with offline_fixture(), patch.dict(os.environ, {
            "PM_PRIVATE_DIR": str(self.private),
            "YANDEX_SEARCH_PAGE_FETCH_ENABLED": "true",
            "YANDEX_SEARCH_PAGE_ALLOWED_DOMAINS": '["example.com"]',
        }), patch("pm.web_search._envelope", side_effect=lambda: original_envelope() | {"source": "synthetic_fixture"}), \
             patch("pm.web_search.now_utc", return_value=timestamp), \
             patch("pm.yandex_search.pages.now_utc", return_value=timestamp), \
             patch("pm.yandex_search.pages.resolve_public"), \
             patch("pm.yandex_search.pages.time.sleep"), \
             patch("pm.yandex_search.pages._request", side_effect=[
                 (200, None, b"User-agent: *\nAllow: /"), (200, None, fixture.read_text(encoding="utf-8")),
             ]) as pages:
            result = app.api("/api/analyze", {
                "query": "Почему снизились заказы, что видно у публичных конкурентов?",
                "web": OFFLINE_INPUT,
            })
        self.assertEqual(pages.call_count, 2)
        self.assertEqual(result["sources_status"]["competitor_web"]["status"], "ok")
        evidence = [item for item in result["evidence"] if item["kind"] == "competitor_web"]
        self.assertEqual({item["quality"] for item in evidence}, {"page_excerpt", "snippet"})
        page = next(item for item in evidence if item["quality"] == "page_excerpt")
        self.assertEqual(page["offer"]["value"], 1050.5)
        self.assertEqual(page["data_mode"], "synthetic")
        batch = qwen.bounded_evidence(evidence, 3200, 180)
        self.assertIn(page["id"], {item["id"] for item in batch})
        self.assertTrue(all(len(item["text"]) <= 180 and item["locator"].startswith("https://") for item in batch))
        for value in ("<script>", "Ignore these instructions", "synthetic-placeholder-key"):
            self.assertNotIn(value, json.dumps(batch, ensure_ascii=False))
        with ai_store.database() as db:
            count = db.execute("SELECT count(*) FROM memory_run_evidence WHERE run_id=? AND observation_id IS NOT NULL "
                               "AND kind='competitor_web'", (result["run_id"],)).fetchone()[0]
            self.assertEqual(count, 2)
        self.assertEqual(history.overview("local")["observations"], 0)

    def test_native_cli_links_same_day_versions_and_replays_after_multi_year_import(self):
        store = History(self.folder)
        start = datetime(2024, 11, 20, tzinfo=UTC)
        states = []
        for price, instant in (
            ("1000", start), ("1200", start + timedelta(hours=3)),
            ("1400", datetime(2026, 11, 20, tzinfo=UTC)),
        ):
            report = capture(history=store, plan=OFFLINE_INPUT, mode="offline",
                             provider=lambda: {"schema_version": 1, "source": "yandex_search", "status": "ok",
                                               "usage": {"requests": 0}, "items": [], "warnings": [],
                                               "reason": None, "retrieved_at": instant.isoformat()},
                             local=[study("study-" + price, "Демо A", price=price,
                                          study_date=instant.date().isoformat())],
                             own=OWN, as_of=instant.date(), now=instant)
            states.append(report["snapshot"]["id"])

        def ingest(after, limit):
            output = subprocess.check_output([
                sys.executable, str(ROOT / "scripts/search_pim_import.py"), "--history-dir", str(self.folder),
                "--mode", "synthetic", "--after", str(after), "--limit", str(limit),
            ], cwd=ROOT, env={**os.environ, "PYTHONUTF8": "1"})
            rows = [json.loads(line) for line in output.splitlines()]
            return rows[:-1], rows[-1]["next_cursor"]

        first, cursor = ingest(0, 2)
        last, _ = ingest(cursor, 2)
        self.assertEqual([item["snapshot"] for item in first + last], states)
        self.assertTrue(all(item["created"] > 0 for item in first + last))
        replay, _ = ingest(0, 3)
        self.assertTrue(all(item["created"] == 0 for item in replay))

        with ai_store.database() as db:
            rows = db.execute("""
                SELECT o.number,o.version,o.payload FROM memory_observations o
                JOIN memory_series s ON s.id=o.series_id
                JOIN memory_sources x ON x.id=s.source_id
                WHERE x.mode='synthetic' AND s.metric='price'
                ORDER BY o.start,o.version
            """).fetchall()
            self.assertEqual([(row["number"], row["version"]) for row in rows],
                             [(1000, 1), (1200, 2), (1400, 1)])
            self.assertEqual(json.loads(rows[-1]["payload"])["previous_snapshot_id"], states[1])
        changes = history.competitor_changes({"mode": "synthetic", "product": OFFLINE_INPUT["product"],
                                              "period": ["2024-11-20", "2026-11-20"]})
        self.assertEqual([(row["before"], row["after"]) for row in changes], [(1200, 1400)])
        self.assertEqual(history.overview("local")["observations"], 0)
