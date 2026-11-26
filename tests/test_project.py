import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from openpyxl import Workbook

import pm.research as research
from pm.analytics import compile_book, effect
from pm.service import metrika, overview, recalculate
from pm.settings import ROOT


class ProductTests(unittest.TestCase):
    def setUp(self):
        folder = self.enterContext(tempfile.TemporaryDirectory())
        names = (
            "PM_WORKBOOK", "PM_CASE_CONFIG", "PM_SNAPSHOT", "PM_EVIDENCE",
            "PM_BENCHMARK", "PM_LLM_URL", "PM_LLM_PROMPT_FILE",
        )
        self.enterContext(patch.dict(os.environ, {**dict.fromkeys(names, ""), "PM_PRIVATE_DIR": folder}))

    def test_periods(self):
        s = overview()
        self.assertEqual([round(x["delta"], 1) for x in s["series"]], [-30, 0])
        self.assertEqual(
            s["reach"]["desktop"], dict(num=168, den=560, days=14, total=28)
        )
        self.assertEqual(
            s["reach"]["mobile"], dict(num=70, den=280, days=14, total=28)
        )
        self.assertEqual(s["release_task"], "TASK-01")
        self.assertEqual(s["backlog"]["id"], "TASK-02")

    def test_workbook_reproduces_demo(self):
        workbook = Path(os.environ["PM_PRIVATE_DIR"]) / "demo.xlsx"
        subprocess.run(
            [sys.executable, str(ROOT / "scripts/demo_workbook.py"), str(workbook)],
            check=True, stdout=subprocess.DEVNULL,
        )
        expected = overview()
        self.assertEqual(compile_book(workbook, source=expected["source"]), expected)

    def test_external_snapshot_and_corpus(self):
        folder = Path(os.environ["PM_PRIVATE_DIR"])
        snapshot, corpus = folder / "snapshot.json", folder / "corpus.json"
        sample = overview()
        sample["source"] = "QA snapshot"
        snapshot.write_text(json.dumps(sample), encoding="utf8")
        corpus.write_text("[]", encoding="utf8")
        with patch.dict(os.environ, {"PM_SNAPSHOT": str(snapshot), "PM_EVIDENCE": str(corpus)}):
            self.assertEqual(overview(), sample)
            self.assertEqual(research.sources(), [])

    def test_synthetic_flat_and_missing(self):
        s = overview()
        for p in s["series"][0]["points"]:
            p[1] = 100
        self.assertEqual(recalculate(s, {})["series"][0]["delta"], 0)
        with self.assertRaises(ValueError):
            recalculate(s, {"after_start": "2025-03-03"})
        s["series"][0]["points"].pop(-2)
        with self.assertRaises(ValueError):
            recalculate(s, {})

    def test_measurement(self):
        data = json.loads(
            (research.ROOT / "data/effect-demo.json").read_text(encoding="utf8")
        )
        rows = data["rows"]
        fix = data["fix"]
        self.assertEqual(effect(rows, fix, 115)["kaspersky"], 120)
        self.assertEqual(effect(rows, fix, 115)["before_fix"]["kaspersky"], 70)
        self.assertEqual(effect(rows, fix, 125)["status"], "Пересмотреть гипотезу")
        partial = deepcopy(rows)
        partial[-1]["complete"] = False
        for incomplete in ([], rows[:-1], partial):
            self.assertIn("Ожидаем", effect(incomplete, fix, 150)["status"])
        with self.assertRaises(ValueError):
            effect(rows + [rows[0]], fix, 150)

    def test_local_sources(self):
        with tempfile.TemporaryDirectory() as folder:
            dest = Path(folder) / "sources.json"
            file = Path(folder) / "sample.xlsx"
            b = Workbook()
            b.active.title = "Ответы"
            b.active["L9"] = "Синтетическая проверка эквайринга"
            b.save(file)
            for _ in range(2):
                self.assertEqual(
                    research.index_range(file, "Ответы", "L9:L9", "Интервью", dest), 1
                )
            records = json.loads(dest.read_text(encoding="utf8"))
            self.assertEqual(len(records), 1)
            self.assertIn("L9", research.search("эквайринг", records)[0]["ref"])
            with self.assertRaises(ValueError):
                research.index_range(file, "Эталонный_кейс", "L9:L9", "Интервью", dest)

    def test_no_model_and_empty_search(self):
        with patch.dict(os.environ, {"PM_LLM_URL": ""}):
            self.assertIsNone(research.analyze("корзина", overview())["model"])
            self.assertEqual(research.analyze("zzzfiction", overview())["hits"], [])
        with self.assertRaises(ValueError):
            research.analyze("корзина", overview(), "https://example.org")

    def test_meter_missing(self):
        with patch.dict(os.environ, {"METRIKA_TOKEN": "", "METRIKA_COUNTER_ID": ""}):
            self.assertFalse(metrika("2026-10-01", "2026-10-02")["available"])

    def test_meter_reply(self):
        def stub(req, timeout):
            self.assertEqual(req.get_header("Authorization"), "OAuth dummy")
            self.assertIn("ids=123", req.full_url)
            return io.BytesIO(b'{"totals":[0],"sampled":false}')

        with patch.dict(
            os.environ, {"METRIKA_COUNTER_ID": "123", "METRIKA_TOKEN": "dummy"}
        ):
            r = metrika("2026-10-01", "2026-10-02", stub)
            self.assertTrue(r["available"])
            self.assertEqual(r["value"], 0)
            self.assertNotIn("dummy", str(r))

            def denied(req, timeout):
                raise HTTPError(req.full_url, 403, "Denied", {}, None)

            self.assertIn("403", metrika("2026-10-01", "2026-10-02", denied)["message"])

    def test_model_rejects_invented_source(self):
        def fake(req, timeout):
            content = json.dumps(
                {
                    "check": "tracking",
                    "sources": ["T-9999"],
                    "question": "Регистрируется ли событие?",
                }
            )
            return io.BytesIO(
                json.dumps({"choices": [{"message": {"content": content}}]}).encode()
            )

        prompt = Path(os.environ["PM_PRIVATE_DIR"]) / "qa.txt"
        prompt.write_text("QA system message", encoding="utf8")
        with patch.dict(os.environ, {"PM_LLM_PROMPT_FILE": str(prompt)}):
            with patch.object(research, "open_model", fake):
                out = research.analyze(
                    "Событие корзины", overview(), "http://localhost:18081/v1"
                )
            self.assertIsNone(out["model"])
            self.assertTrue(out["hits"])
            self.assertIn("не прошёл", out["notice"])

    def test_static_boot_is_escaped(self):
        import app

        sample = overview()
        sample["source"] = "</script><script>SHOULD_BE_ESCAPED</script>"
        html = app.page(sample)
        self.assertNotIn(sample["source"], html)
        self.assertIn("SHOULD_BE_ESCAPED", html)
