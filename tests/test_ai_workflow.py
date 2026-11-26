import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request, urlopen

import app
from pm import ai_store, investigation, qwen
from pm.analysis_context import public_search, safe_locator
from pm.settings import ROOT


class InvestigationTests(unittest.TestCase):
    def setUp(self):
        self.folder = self.enterContext(tempfile.TemporaryDirectory())
        defaults = dict.fromkeys(("PM_WORKBOOK", "PM_SNAPSHOT", "PM_EVIDENCE",
                                  "PM_LLM_URL", "PM_LLM_PROMPT_FILE", "YANDEX_SEARCH_ENABLED"), "")
        self.enterContext(patch.dict(os.environ, defaults | {"PM_PRIVATE_DIR": self.folder}))

    def test_http_qwen_pipeline_repairs_and_persists(self):
        seen = []

        class Model(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                seen.append(request)
                content = "broken-json" if len(seen) == 1 else json.dumps({"hypotheses": [{
                    "problem": "Падение индекса заказов", "cause": "Возможна потеря события",
                    "verify": "Сверить регистрацию события после выбора лицензии",
                    "expected": "Появится проверяемая последовательность событий",
                    "missing": "Нет разреза корзины", "risk": "Изменение состава аудитории",
                    "evidence_ids": ["orders:0"], "contradictions": ["orders:1"],
                }]})
                response = json.dumps({"choices": [{"message": {"content": content}}],
                                       "usage": {"total_tokens": 100}}).encode()
                self.send_response(200)
                self.end_headers()
                self.wfile.write(response)

        model = ThreadingHTTPServer(("127.0.0.1", 0), Model)
        server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        for target in (model, server):
            threading.Thread(target=target.serve_forever, daemon=True).start()
            self.addCleanup(target.server_close)
            self.addCleanup(target.shutdown)
        with patch.dict(os.environ, {"PM_LLM_URL": f"http://127.0.0.1:{model.server_port}/v1"}):
            endpoint = f"http://127.0.0.1:{server.server_port}/api/analyze"
            req = Request(endpoint, data=b'{"query":"basket"}', headers={"Content-Type": "application/json"})
            with urlopen(req, timeout=10) as reply:
                report = json.load(reply)
        self.assertEqual(report["llm_status"], "ok")
        self.assertEqual(ai_store.run(report["run_id"]), report)
        self.assertEqual(report["hypotheses"][0]["confidence"], "low")
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[0]["response_format"]["type"], "json_schema")
        context = json.loads(seen[0]["messages"][1]["content"])
        self.assertIn("release:current", {item["id"] for item in context["evidence"]})

    def test_fallback_and_idempotent_review(self):
        report = investigation.analyze({"query": "корзина"})
        self.assertEqual(report["llm_status"], "unavailable")
        draft = report["proposed_tasks"][0]
        body = dict(run_id=report["run_id"], proposal_id=draft["id"], action="accept",
                    target=110, control_min=80, idempotency_key="test-key")
        task = ai_store.decide(body)
        self.assertEqual(task, ai_store.decide(body))
        self.assertEqual(len(ai_store.task_list()), 1)
        with self.assertRaises(ValueError):
            ai_store.decide(body | {"target": 111})
        self.assertEqual(ai_store.review({"task_id": task["id"]})["status"], "waiting_for_data")
        demo = json.loads((ROOT / "data/effect-demo.json").read_text(encoding="utf8"))
        review = ai_store.review({"task_id": task["id"], "observation": demo, "mode": "simulation"})
        self.assertEqual(review["status"], "achieved")
        self.assertEqual(review["mode"], "simulation")
        with self.assertRaises(ValueError):
            ai_store.review({"task_id": task["id"], "observation": demo})

    def test_alternative_snapshot(self):
        snapshot = json.loads((ROOT / "data/demo-overview.json").read_text(encoding="utf8"))
        for point in snapshot["series"][0]["points"]:
            point[1] = 100
        path = Path(self.folder) / "snapshot.json"
        path.write_text(json.dumps(snapshot), encoding="utf8")
        with patch.dict(os.environ, {"PM_SNAPSHOT": str(path)}):
            report = investigation.analyze({"query": "корзина"})
        self.assertEqual(report["evidence"][0]["values"]["delta"], 0)
        self.assertEqual(report["hypotheses"], [])
        self.assertEqual(report["data_mode"], "local")

    def test_public_search_is_opt_in_and_validates_v1(self):
        calls = []

        def collector(**kwargs):
            calls.append(kwargs)
            return {"schema_version": 1, "status": "partial", "reason": None, "items": [{
                "id": "web:stable", "kind": "competitor_web", "title": "Catalog",
                "text": "Synthetic excerpt", "url": "https://example.com/catalog",
                "domain": "example.com", "product": "Public product",
                "content_type": "snippet", "topic": "licensing",
            }]}

        scope = {"product": "Public product", "domains": ["example.com"], "topics": ["licensing"]}
        self.assertEqual(public_search(scope, collector)[0], "disabled")
        with patch.dict(os.environ, {
            "YANDEX_SEARCH_ENABLED": "true",
            "YANDEX_SEARCH_ALLOWED_DOMAINS": '["example.com"]',
            "YANDEX_SEARCH_PUBLIC_PRODUCTS": '["Public product"]',
        }):
            self.assertEqual(public_search(scope | {"product": "private"}, collector)[0], "error")
            self.assertEqual(public_search(scope)[1], "adapter_not_installed")
            report = investigation.analyze({"query": "только для локального анализа", "web": scope}, collector=collector)
        self.assertEqual(calls, [scope])
        self.assertEqual(report["sources_status"]["competitor_web"]["status"], "partial")
        self.assertTrue(any(item["quality"] == "snippet" for item in report["evidence"]))

    def test_rejects_unknown_ids_and_blocks_private_paths(self):
        item = dict.fromkeys(qwen.FIELDS, "проверить")
        item.update(evidence_ids=["unknown"], contradictions=[])
        with self.assertRaises(ValueError):
            qwen.parse(json.dumps({"hypotheses": [item]}), {})
        self.assertNotIn("private", safe_locator("C:/private/book.xlsx · A1"))
        with patch.dict(os.environ, {"PM_LLM_URL": "http://example.com/v1"}):
            self.assertEqual(qwen.infer("query", [])[3], "invalid_configuration")
