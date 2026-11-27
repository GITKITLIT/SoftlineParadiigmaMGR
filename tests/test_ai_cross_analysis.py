"""Independent synthetic cases with analytical expectations beyond JSON shape."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pm import ai_store, investigation, monitor
from pm.analysis_context import entry
from pm.cross_analysis import strengthen
from pm.settings import ROOT


class CrossAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ, {
            "PM_PRIVATE_DIR": str(self.folder), "PM_WORKBOOK": "", "PM_SNAPSHOT": "",
            "PM_EVIDENCE": "", "PM_LLM_URL": "", "YANDEX_SEARCH_ENABLED": "",
            "PM_MONITOR_DAILY_LIMIT": "4",
        }))

    def test_calculates_counter_conflict_only_in_comparable_store_windows(self):
        calls = []
        def meter(start, end, *, segment, detailed):
            calls.append((start, end))
            return {"status": "ok", "scope": "store_counter", "period": {"start": start, "end": end},
                    "metrics": {"visits": {"unit": "visits", "metric": "ym:s:visits", "value": 100 if len(calls) == 1 else 120}}}

        report = investigation.analyze({"query": "Изменение заказов и визитов", "metrika": {"compare": True}}, meter=meter)
        self.assertEqual(calls, [("2025-02-03", "2025-03-02"), ("2025-03-10", "2025-04-06")])
        conflict = next(x for x in report["findings"] if x.get("relation") == "sales_metrika")
        self.assertAlmostEqual(conflict["values"]["orders_delta"], -30)
        self.assertAlmostEqual(conflict["values"]["metrika_delta"], 20)
        self.assertEqual(set(conflict["contradictions"]), {"metrika:before:visits", "metrika:after:visits"})
        self.assertTrue(any(x.get("relation") == "sales_release" and x["statement_type"] == "assumption"
                            for x in report["findings"]))
        def test_counter(*args, **kwargs):
            return meter(*args, **kwargs) | {"scope": "demo_counter"}
        demo = investigation.analyze({"query": "Изменение заказов и визитов", "metrika": {"compare": True}}, meter=test_counter)
        self.assertNotIn("sales_metrika", [x.get("relation") for x in demo["findings"]])

    def test_links_interview_competitor_existing_task_and_flags_old_source(self):
        corpus = [
            {"id": "voice", "kind": "Интервью", "ref": "Синтетическое интервью", "text": "Корзина скрывает условия заказа"},
            {"id": "competitor", "kind": "Конкуренты", "ref": "Синтетический конкурент",
             "text": "У конкурента корзина показывает условия заказа", "observed_at": "2019-02-02"},
        ]
        file = self.folder / "local.json"
        file.write_text(json.dumps(corpus, ensure_ascii=False), encoding="utf8")
        with patch.dict(os.environ, {"PM_EVIDENCE": str(file)}):
            report = investigation.analyze({"query": "Как проверить корзину?"})
        self.assertEqual(next(x for x in report["evidence"] if x["kind"] == "competitor_local")["freshness"], "stale")
        kinds = {item.get("relation"): item for item in report["findings"]}
        for code in ("research_and_competitors", "competitors_and_backlog"):
            self.assertEqual(len(kinds[code]["evidence_ids"]), 2)
            self.assertEqual(kinds[code]["statement_type"], "assumption")
            self.assertTrue(kinds[code]["missing"])
        self.assertTrue(any("актуальность" in item for item in report["limitations"]))

    def test_copies_and_stale_sources_do_not_raise_confidence(self):
        shared = [entry("a", "orders", "Series A", mode="local", source_group="shared"),
                  entry("b", "orders", "Series B", mode="local", source_group="shared"),
                  entry("c", "competitor_local", "Old study", mode="local", source_group="other", freshness="stale")]
        draft = {"evidence_ids": ["a", "b", "c"], "contradictions": []}
        strengthen([draft], shared)
        self.assertEqual(draft["independent_sources"], 1)
        self.assertEqual(draft["confidence"], "low")

    def test_failed_meter_and_no_control_leave_limitations(self):
        snapshot = json.loads((ROOT / "data/demo-overview.json").read_text(encoding="utf8"))
        snapshot["series"] = snapshot["series"][:1]
        file = self.folder / "one-series.json"
        file.write_text(json.dumps(snapshot), encoding="utf8")
        def offline(*args, **kwargs):
            raise TimeoutError("look up result")
        with patch.dict(os.environ, {"PM_SNAPSHOT": str(file)}):
            report = investigation.analyze({"query": "Падение заказов", "metrika": {"compare": True}}, meter=offline)
        self.assertEqual(report["sources_status"]["metrika"]["status"], "unavailable")
        self.assertIn("Контрольная группа отсутствует.", report["limitations"])
        self.assertNotIn("sales_control", [x.get("relation") for x in report["findings"]])

    def test_proactive_investigates_notifies_once_without_approving_a_task(self):
        first = monitor.check({}, proactive=True)
        second = monitor.check({}, proactive=True)
        self.assertTrue(first["notification"])
        self.assertFalse(second["notification"])
        self.assertEqual(ai_store.notifications()[0]["run_id"], first["run_id"])
        self.assertEqual(len(ai_store.recent()), 1)
        self.assertTrue(ai_store.run(first["run_id"])["proposed_tasks"])
        self.assertEqual(ai_store.task_list(), [])

    def test_proactive_daily_limit_and_incomplete_window(self):
        with patch.dict(os.environ, {"PM_MONITOR_DAILY_LIMIT": "1"}):
            first = monitor.check({"alert_percent": 10}, proactive=True)
            second = monitor.check({"alert_percent": 11}, proactive=True)
            incomplete = monitor.check({"after_end": "2025-03-10"}, proactive=True)
        self.assertIn("run_id", first)
        self.assertNotIn("run_id", second)
        self.assertEqual(incomplete["status"], "missing")
        self.assertEqual(len(ai_store.recent()), 1)


    def test_joint_investigation_keeps_cross_checks_and_public_boundary(self):
        file = self.folder / "corpus.json"
        file.write_text(json.dumps([
            {"id": "interview", "kind": "Интервью", "ref": "Синтетический CustDev",
             "text": "Визиты есть, но корзина скрывает условия заказа"},
            {"id": "study", "kind": "Конкуренты", "ref": "Синтетическое исследование",
             "text": "Корзина конкурента показывает условия заказа",
             "observed_at": "2025-03-15"},
        ], ensure_ascii=False), encoding="utf8")
        def meter(start, end, **parameters):
            return {"status": "ok", "scope": "store_counter", "period": {"start": start, "end": end},
                    "metrics": {"visits": {"unit": "visits", "metric": "ym:s:visits",
                                          "value": 100 if start < "2025-03-01" else 130}}}
        calls = []
        def provider(**public_fields):
            calls.append(public_fields)
            return {"schema_version": 1, "source": "synthetic_fixture", "status": "ok", "reason": None,
                    "items": [{"id": "web:synthetic", "kind": "competitor_web", "title": "Корзина",
                               "text": "Синтетическое публичное описание корзины",
                               "url": "https://example.com/catalog", "domain": "example.com",
                               "product": "Public Demo", "topic": "checkout", "content_type": "snippet"}]}
        scopes = {"product": "Public Demo", "domains": ["example.com"], "topics": ["checkout"]}
        actions = iter(("sales", "metrika", "releases", "research", "competitors", "backlog", "public_competitors", "funnel"))
        def decide(query, available, evidence, trace, deadline):
            return {"tool": next(actions), "arguments": {}, "purpose": "compare"}, {}
        with patch.dict(os.environ, {"PM_EVIDENCE": str(file), "YANDEX_SEARCH_ENABLED": "true",
                                    "YANDEX_SEARCH_PUBLIC_PRODUCTS": '["Public Demo"]',
                                    "YANDEX_SEARCH_ALLOWED_DOMAINS": '["example.com"]'}):
            report = investigation.analyze({
                "query": "Заказы, визиты, интервью, конкуренты, релиз, бэклог и корзина",
                "metrika": {"compare": True}, "web": scopes,
            }, collector=provider, meter=meter, decide=decide)
        self.assertEqual(calls, [scopes])
        self.assertEqual({row["kind"] for row in report["evidence"]},
                         {"orders", "funnel", "release", "interview", "backlog",
                          "competitor_local", "competitor_web", "metrika"})
        self.assertEqual(next(x for x in report["evidence"] if x["id"] == "web:synthetic")["data_mode"], "synthetic")
        self.assertTrue({"sales_metrika", "sales_release", "metrics_and_research", "research_and_competitors",
                         "competitors_and_backlog"} <= {row.get("relation") for row in report["findings"]})


    def test_public_empty_and_timeout_preserve_local_analysis(self):
        scope = {"product": "Public Demo", "domains": ["example.com"], "topics": ["support"]}
        with patch.dict(os.environ, {"YANDEX_SEARCH_ENABLED": "true",
                                    "YANDEX_SEARCH_PUBLIC_PRODUCTS": '["Public Demo"]',
                                    "YANDEX_SEARCH_ALLOWED_DOMAINS": '["example.com"]'}):
            request = {"query": "Падение заказов", "web": scope}
            empty = investigation.analyze(request, collector=lambda **kwargs: {
                "schema_version": 1, "status": "ok", "items": [],
            })
            def unavailable(**kwargs):
                raise TimeoutError("Synthetic timeout")
            failed = investigation.analyze(request, collector=unavailable)
        self.assertEqual(empty["sources_status"]["competitor_web"]["status"], "ok")
        self.assertEqual(failed["sources_status"]["competitor_web"]["status"], "error")
        self.assertFalse(any(row["kind"] == "competitor_web" for row in empty["evidence"]))
        self.assertEqual(empty["effect_plan"]["baseline"], failed["effect_plan"]["baseline"])
        self.assertTrue(failed["findings"])
