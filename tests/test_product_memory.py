import concurrent.futures
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import app
from pm import ai_store, collection, history, investigation, memory, qwen
from pm.agent import valid_action
from pm.settings import ROOT
from scripts import pim_backup


def point(day, amount, *, product="Demo A", mode="synthetic", **extra):
    return dict(mode=mode, stream="sales", source="two-year-fixture", product=product,
                metric="orders", unit="orders", aggregation="sum", method="same-method",
                timezone="UTC", start=day, value=amount, quality="complete") | extra


class ProductMemoryTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.active = self.folder / "state"
        self.enterContext(patch.dict(os.environ, {
            "PM_PRIVATE_DIR": str(self.active), "PM_WORKBOOK": "", "PM_SNAPSHOT": "",
            "PM_EVIDENCE": "", "PM_LLM_URL": "", "YANDEX_SEARCH_ENABLED": "",
        }))

    def test_two_years_multiple_products_comparing_year_and_quarter(self):
        first = date(2024, 1, 1)
        rows = []
        for offset in range(731):
            current = first + timedelta(days=offset)
            for product, scale in (("Demo A", 1), ("Demo B", 2)):
                rows.append(point(current.isoformat(), scale * (10 if current.year == 2024 else 12),
                                  product=product))
        result = memory.write_observations(rows)
        self.assertEqual(result["created"], len(rows))
        self.assertEqual(memory.write_observations(rows)["created"], 0)
        self.assertEqual(len(history.series_index("synthetic")), 2)
        row = history.series_index("synthetic", product="Demo A")[0]
        same_day = history.compare(dict(mode="synthetic", series_id=row["id"],
                                      before=["2024-10-01", "2024-10-01"],
                                      after=["2025-10-01", "2025-10-01"]))
        block = history.compare(dict(mode="synthetic", series_id=row["id"],
                                   before=["2024-10-01", "2024-12-31"],
                                   after=["2025-10-01", "2025-12-31"]))
        self.assertEqual(same_day["delta"], 2)
        self.assertEqual(block["delta"], 184)
        self.assertAlmostEqual(block["delta_percent"], 20)
        self.assertEqual(history.overview("local")["observations"], 0)

    def test_as_of_retrieves_old_version_and_partial_refuses_false_delta(self):
        with ai_store.database() as db:
            first, _ = memory.observation(db, point("2024-10-01", 10), recorded="2025-02-01T00:00:00+00:00")
            second, _ = memory.observation(db, point("2024-10-01", 12), recorded="2025-03-01T00:00:00+00:00")
            memory.observation(db, point("2025-10-01", None), recorded="2025-10-02T00:00:00+00:00")
            saved = db.execute("SELECT version,supersedes FROM memory_observations WHERE id=?", (second,)).fetchone()
        self.assertEqual(tuple(saved), (2, first))
        series = history.series_index("synthetic")[0]["id"]
        old = history.timeline({"mode": "synthetic", "series_id": series, "period": ["2024-01-01", "2025-12-31"],
                                "as_of": "2025-02-02T00:00:00+00:00"})
        self.assertEqual(old["observations"][0]["number"], 10)
        compare = history.compare({"mode": "synthetic", "series_id": series,
                                   "before": ["2024-10-01", "2024-10-01"], "after": ["2025-10-01", "2025-10-01"]})
        self.assertEqual(compare["status"], "incomparable")
        self.assertIsNone(compare["delta"])
        with self.assertRaises(ValueError):
            history.timeline({"mode": "local", "series_id": series, "period": ["2024-01-01", "2025-12-31"]})

    def test_today_is_incomplete_even_when_operator_mislabeled_value(self):
        today = date.today()
        before = (today - timedelta(days=364)).isoformat()
        after = today.isoformat()
        memory.write_observations([point(before, 10, mode="local"), point(after, 12, mode="local")])
        sid = history.series_index("local")[0]["id"]
        initial = history.compare(dict(series_id=sid, before=[before, before], after=[after, after]))
        self.assertEqual(initial["after"]["status"], "partial_day")
        self.assertIsNone(initial["delta"])

    def test_competitor_history_separates_currencies_and_ignores_snippets(self):
        def fake(when, host, value, currency="RUB", kind="page_excerpt"):
            return dict(id=f"web:{host}", locator=f"https://{host}/price", product="Demo offer",
                        competitor=host, topic="pricing", data_mode="synthetic", quality=kind,
                        retrieved_at=when + "T12:00:00Z", observed_at=None,
                        offer={"metric": "price", "currency": currency, "terms": "annual", "value": value})
        with ai_store.database() as db:
            for item in (fake("2024-02-01", "one.example", 100), fake("2025-02-01", "one.example", 120),
                         fake("2025-03-01", "one.example", 20, currency="USD"),
                         fake("2024-02-01", "two.example", 60), fake("2025-02-01", "two.example", 70, kind="snippet")):
                memory.record_web(db, item)
        rows = history.competitor_changes({"mode": "synthetic", "period": ["2024-01-01", "2025-12-31"]})
        self.assertEqual([(row["competitor"], row["before"], row["after"], row["currency"]) for row in rows],
                         [("one.example", 100, 120, "RUB")])
        self.assertIsNone(rows[0]["published"])

    def test_historical_document_and_rejected_hypothesis_survive_restart(self):
        ai_store.store_excerpts("interview", [{"id": "a", "ref": "Synthetic study", "kind": "Интервью",
                                              "text": "Оплата задерживается, пароль: redacted_example",
                                              "product": "Demo A", "observed_at": "2024-06-01"}])
        ai_store.store_excerpts("interview", [{"id": "a", "ref": "Synthetic study", "kind": "Интервью",
                                              "text": "Оплата появилась", "product": "Demo A"}])
        report = investigation.analyze({"query": "Снижение заказов"})
        proposal = report["proposed_tasks"][0]["id"]
        ai_store.decide({"run_id": report["run_id"], "proposal_id": proposal, "action": "reject",
                         "idempotency_key": "reject", "reason": "Нет подтверждения"})
        statement = "from pm import history; import json; print(json.dumps(history.past_decisions('synthetic','заказ'),ensure_ascii=False))"
        result = subprocess.run([sys.executable, "-B", "-c", statement], cwd=ROOT, check=True,
                                capture_output=True, encoding="utf8")
        rows = json.loads(result.stdout)
        self.assertEqual(rows[0]["decision"], "reject")
        self.assertEqual(rows[0]["decision_reason"], "Нет подтверждения")
        results = history.search({"query": "оплата", "product": "Demo A", "mode": "local"})
        self.assertEqual({row["revision"] for row in results["documents"]}, {1, 2})
        self.assertNotIn("redacted_example", json.dumps(results))
        self.assertFalse(history.search({"query": "оплата", "mode": "synthetic"})["documents"])

    def test_agent_chooses_fixed_historical_tool_without_sql_or_promotion(self):
        memory.write_observations([point("2024-01-01", 20), point("2025-01-01", 25)])
        row = history.series_index("synthetic")[0]["id"]
        choices = iter(("compare_history", "finish"))
        def chooser(query, available, evidence, trace, deadline):
            selected = next(choices)
            return {"tool": selected, "arguments": {},
                    "purpose": "compare" if selected != "finish" else "enough_evidence"}, {}
        answer = investigation.analyze({
            "query": "Сравни два периода", "memory": {"mode": "synthetic", "compare":
                {"series_id": row, "before": ["2024-01-01", "2024-01-01"], "after": ["2025-01-01", "2025-01-01"]}},
        }, decide=chooser)
        self.assertEqual(next(row for row in answer["findings"] if row["relation"] == "historical_comparison")["values"]["delta"], 5)
        self.assertFalse(answer["hypotheses"])
        self.assertFalse(any(item["kind"] in {"orders", "funnel"} for item in answer["evidence"]))
        self.assertFalse(valid_action({"tool": "compare_history", "arguments": {"sql": "SELECT 1"},
                                      "purpose": "compare"}, {"compare_history": ""}))
        self.assertFalse(valid_action({"tool": "delete_backup", "arguments": {}, "purpose": "compare"}, {}))
        synthetic_hypothesis = """{"hypotheses":[{"cause":"Это прошлое предположение","verify":"Сверить следы событий","expected":"Окна различаются","evidence_ids":["prior"],"contradictions":[]}]}"""
        with self.assertRaises(ValueError):
            qwen.parse(synthetic_hypothesis, {"prior": {"kind": "memory_context"}})

    def test_collection_jobs_dedupe_and_atomic_rollback(self):
        first = collection.acquire("sales", "test-key")
        self.assertIsNotNone(first)
        self.assertIsNone(collection.acquire("sales", "test-key"))
        collection.settle(first, "ok")
        self.assertIsNone(collection.acquire("sales", "test-key"))
        with self.assertRaises(ValueError):
            memory.write_observations([point("2024-01-01", 5), point("bad-date", 6)])
        self.assertFalse(history.series_index("synthetic"))

    def test_active_backup_restore_links_and_future_writes(self):
        memory.write_observations([point("2024-01-01", 25), point("2025-01-01", 30)])
        initial_id = history.series_index("synthetic")[0]["id"]
        ai_store.store_excerpts("research", [{"id": "x", "kind": "Интервью", "ref": "Example",
                                              "text": "Каталог открыт"}])
        report = investigation.analyze({"query": "Причины падения заказов"})
        ai_store.decide({"run_id": report["run_id"], "proposal_id": report["proposed_tasks"][0]["id"],
                         "action": "reject", "idempotency_key": "decided"})
        source = self.active / pim_backup.DB_NAME
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            future = pool.submit(pim_backup.create, source, self.folder / "backups")
            results = list(pool.map(lambda i: memory.write_observations([point(f"2026-01-{i:02}", 5)]), range(1, 12)))
            backup, info = future.result(10)
        self.assertTrue(all(row["created"] for row in results))
        restore_dir = self.folder / "restore"
        result = pim_backup.restore(backup, restore_dir)
        self.assertGreaterEqual(result["restore_seconds"], 0)
        self.assertEqual(pim_backup.verify(backup)["sqlite_version"], ai_store.SCHEMA_VERSION)
        with patch.dict(os.environ, {"PM_PRIVATE_DIR": str(restore_dir)}):
            latest = history.timeline({"mode": "synthetic", "series_id": initial_id,
                                       "period": ["2024-01-01", "2025-01-01"]})
            self.assertEqual([r["number"] for r in latest["observations"]], [25, 30])
            self.assertEqual(ai_store.task_list()[0]["run_id"], report["run_id"])
            self.assertTrue(history.search({"mode": "local", "query": "каталог"})["documents"])
            memory.write_observations([point("2027-01-01", 31)])
            self.assertTrue(any(row["id"] == initial_id for row in
                                app.api("/api/memory/series", {"mode": "synthetic"})["series"]))
        with self.assertRaises(FileExistsError):
            pim_backup.restore(backup, self.active)
        with (backup / pim_backup.DB_NAME).open("ab") as f:
            f.write(b"synthetic corruption")
        with self.assertRaises(ValueError):
            pim_backup.verify(backup)

    def test_failed_migration_rolls_back_ddl_and_preserves_previous_data(self):
        self.active.mkdir()
        database = self.active / pim_backup.DB_NAME
        with closing(sqlite3.connect(database)) as db:
            db.execute("CREATE TABLE runs(id TEXT PRIMARY KEY, created TEXT, report TEXT)")
            db.execute("CREATE TABLE imported_evidence(id TEXT PRIMARY KEY, collection TEXT, body TEXT)")
            db.execute("INSERT INTO runs VALUES ('existing','2025-01-01','{}')")
            db.execute("PRAGMA user_version=4")
            db.commit()
        def broken(db):
            db.execute("CREATE TABLE half_created(id INTEGER)")
            raise RuntimeError("Synthetic migration error")
        with patch.object(memory, "migrate", broken), self.assertRaises(RuntimeError):
            with ai_store.database():
                pass
        with closing(sqlite3.connect(database)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 4)
            self.assertEqual(db.execute("SELECT id FROM runs").fetchone()[0], "existing")
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='half_created'").fetchone())


    def test_fixed_product_timeline_preserves_releases_across_years(self):
        memory.write_observations([
            point("2024-06-03", None, stream="release", metric="release", unit="event", aggregation="snapshot",
                  subject="release-a", event_date="2024-06-03"),
            point("2025-06-02", None, stream="release", metric="release", unit="event", aggregation="snapshot",
                  subject="release-b", event_date="2025-06-02"),
        ])
        history_result = history.product_history({"mode": "synthetic", "product": "Demo A",
                                                 "period": ["2024-01-01", "2025-12-31"]})
        self.assertEqual([item["id"] for item in history_result["releases"]], ["release-b", "release-a"])

    def test_annotations_are_unconfirmed_and_contacts_rejected(self):
        self.assertEqual(history.save_note({"text": "Конкурент показал оплату", "product": "Demo A"})["status"], "reported")
        results = history.search({"mode": "local", "query": "оплата конкурент"})
        self.assertEqual(json.loads(results["documents"][0]["metadata"])["origin"], "user_reported")
        with self.assertRaises(ValueError):
            history.save_note({"text": "Контакт someone@example.com"})

    def test_uncommitted_subprocess_write_does_not_replace_existing_data(self):
        memory.write_observations([point("2024-01-01", 10)])
        initial = history.overview("synthetic")["observations"]
        database = self.active / pim_backup.DB_NAME
        child_code = (
            "import os,sqlite3,sys; c=sqlite3.connect(sys.argv[1]); "
            "c.execute('BEGIN IMMEDIATE'); c.execute('UPDATE memory_observations SET number=99'); os._exit(7)"
        )
        result = subprocess.run([sys.executable, "-B", "-c", child_code, str(database)], check=False)
        self.assertEqual(result.returncode, 7)
        sid = history.series_index("synthetic")[0]["id"]
        row = history.timeline({"mode": "synthetic", "series_id": sid, "period": ["2024-01-01", "2024-01-01"]})
        self.assertEqual(row["observations"][0]["number"], 10)
        self.assertEqual(history.overview("synthetic")["observations"], initial)

    def test_progress_is_visible_while_worker_waits_and_is_limited(self):
        from threading import Event
        from pm import live_runs

        ready, release = Event(), Event()
        def fake(body, progress):
            progress({"step": "tool", "tool": "compare_history", "status": "running", "password": "ignored"})
            ready.set()
            if not release.wait(4):
                raise ValueError
            return {"run_id": "synthetic-final"}
        identifier = live_runs.start({"query": "Synthetic"}, analyze=fake)["job_id"]
        try:
            self.assertTrue(ready.wait(4))
            status = live_runs.status(identifier)
            self.assertEqual(status["status"], "running")
            self.assertEqual(status["phases"], [{"step": "tool", "tool": "compare_history", "status": "running"}])
        finally:
            release.set()
        from time import monotonic, sleep
        until = monotonic() + 4
        while monotonic() < until and live_runs.status(identifier)["status"] == "running":
            sleep(.01)
        self.assertEqual(live_runs.status(identifier)["run_id"], "synthetic-final")

    def test_v1_structured_attributes_are_preserved_without_guessing_terms(self):
        from pm.analysis_context import normalize_web

        v1 = {"id": "web:demo", "url": "https://example.com/product", "domain": "example.com", "product": "Demo",
              "topic": "pricing", "content_type": "page_excerpt", "kind": "competitor_web", "title": "Synthetic",
              "text": "Public synthetic offering", "retrieved_at": "2025-08-01T00:00:00Z",
              "attributes": {"product": "Demo", "version": "v1", "license_type": "business", "duration": "year",
                             "devices": "1", "tax": "inc", "price": "2200,00", "currency": "RUB"}}
        normalized = normalize_web(v1, ["example.com"], "Demo", ["pricing"])
        self.assertEqual(normalized["offer"]["value"], 2200)
        self.assertNotIn("offer", normalize_web(v1 | {"attribute_conflicts": {"tax": ["inc", "exc"]}},
                                               ["example.com"], "Demo", ["pricing"]))

    def test_ratio_uses_original_counts_and_preserves_percent_units(self):
        for day, orders, traffic in (("2024-01-01", 10, 100), ("2025-01-01", 20, 160)):
            memory.write_observations([point(day, orders), point(day, traffic, metric="visits", unit="visits")])
        by_name = {row["metric"]: row["id"] for row in history.series_index("synthetic")}
        request = dict(mode="synthetic", series_id=by_name["orders"], denominator_series_id=by_name["visits"],
                       before=["2024-01-01", "2024-01-01"], after=["2025-01-01", "2025-01-01"])
        self.assertEqual(history.compare(request)["delta"], 2.5)
        self.assertEqual(history.compare(request)["series"]["unit"], "percent")


    def test_server_migration_requires_independent_status_marker(self):
        self.active.mkdir()
        database = self.active / pim_backup.DB_NAME
        with closing(sqlite3.connect(database)) as db:
            db.execute("CREATE TABLE runs(id TEXT PRIMARY KEY, created TEXT, report TEXT)")
            db.execute("CREATE TABLE imported_evidence(id TEXT PRIMARY KEY, collection TEXT, body TEXT)")
            db.execute("PRAGMA user_version=4")
            db.commit()
        status = self.folder / "status.json"
        with patch.dict(os.environ, {"PM_MIGRATION_STATUS_FILE": str(status), "PM_BACKUP_STATUS_FILE": str(status)}):
            with self.assertRaisesRegex(ValueError, "проверенной независимой"):
                with ai_store.database():
                    pass
            pim_backup.create(database, self.folder / "snapshots", status=status)
            with ai_store.database() as db:
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], ai_store.SCHEMA_VERSION)
            offered = history.backup_status()
            self.assertEqual(set(offered), {"status", "last_backup", "last_integrity_check"})
            self.assertNotIn("source_mtime_ns", json.dumps(offered))

    def test_email_migration_keeps_prior_journal_and_does_not_enqueue_old_reports(self):
        from pm import monitor

        with patch.dict(os.environ, {"PM_EMAIL_ENABLED": "false"}):
            result = monitor.check({}, proactive=True)
        with ai_store.database() as db:
            db.execute("DROP TABLE email_outbox")
            db.execute("DROP TABLE email_daily_usage")
            db.execute("PRAGMA user_version=5")
        source = self.active / pim_backup.DB_NAME
        status = self.folder / "status.json"
        with patch.dict(os.environ, {"PM_MIGRATION_STATUS_FILE": str(status)}):
            with self.assertRaisesRegex(ValueError, "проверенной независимой"):
                ai_store.notifications()
            pim_backup.create(source, self.folder / "snapshots", status=status)
            row = ai_store.notifications()[0]
            self.assertEqual(row["run_id"], result["run_id"])
            self.assertIsNone(row["email_status"])
            with ai_store.database() as db:
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], ai_store.SCHEMA_VERSION)
                self.assertEqual(db.execute("SELECT count(*) FROM email_outbox").fetchone()[0], 0)

    def test_rejected_decision_is_not_reproposed_without_new_evidence(self):
        request = {"query": "Почему упали заказы?"}
        first = investigation.analyze(request)
        declined = first["proposed_tasks"][0]
        ai_store.decide({"run_id": first["run_id"], "proposal_id": declined["id"], "action": "reject",
                         "idempotency_key": "same-proposal", "reason": "Нужны новые данные"})
        second = investigation.analyze(request)
        self.assertNotIn(declined["hypothesis"], [item["hypothesis"] for item in second["proposed_tasks"]])
        self.assertTrue(any("отклонённое решение" in value for value in second["limitations"]))


    def test_release_effect_excludes_release_week_from_historical_totals(self):
        launch = date(2025, 6, 2)
        days = [launch + timedelta(days=i) for i in range(-28, 35)]
        rows = [point(day.isoformat(), 10 if day < launch else 999 if day < launch + timedelta(days=7) else 12)
                for day in days]
        rows.append(point(launch.isoformat(), None, stream="release", metric="release", unit="event",
                          aggregation="snapshot", event_date=launch.isoformat(), subject="test-release"))
        memory.write_observations(rows)
        sid = next(row["id"] for row in history.series_index("synthetic") if row["stream"] == "sales")
        releases = history.product_history({"mode": "synthetic", "product": "Demo A", "series_id": sid,
                                            "period": ["2025-01-01", "2025-12-31"]})["releases"]
        comparison = releases[0]["comparison"]
        self.assertEqual(comparison["before"]["value"], 280)
        self.assertEqual(comparison["after"]["value"], 336)
        self.assertEqual(comparison["delta"], 56)
        self.assertEqual(comparison["interpretation"], "descriptive_only")
