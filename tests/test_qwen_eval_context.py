"""Regression checks for constrained qualitative inference and tool scope."""

import io
import json
import unittest
from unittest.mock import Mock, patch

from llm_lab.synthesis import qualitative_input
from pm import agent, chat, qwen
from pm.analysis_context import TOOLS, entry


class ContextTests(unittest.TestCase):
    def test_qualitative_projection_keeps_fact_directions_and_evidence_ids(self):
        evidence = [{"id": "orders:0", "kind": "orders", "ref": "Demo A",
                     "text": "Период 2025-04-21. Выпуск R-19.", "quality": "observed",
                     "values": {"before": 100, "after": 70, "delta": -30}}]
        report = qualitative_input("Сравнить R-19", evidence, [])
        self.assertEqual(report["question"], "Сравнить")
        self.assertEqual(report["evidence"][0]["id"], "orders:0")
        self.assertEqual(report["evidence"][0]["cues"], {"изменение": "снижение"})
        self.assertNotRegex(report["evidence"][0]["text"], r"\d")
        self.assertEqual(evidence[0]["values"]["before"], 100)

    def test_unknown_action_and_extra_arguments_never_reach_tool(self):
        backend = Mock()
        backend.query, backend.available, backend.evidence, backend.completed = "Продажи", {"sales": TOOLS["sales"]}, {}, set()
        backend.run.return_value = {"status": "ok", "evidence_ids": ["orders:0"]}
        fake = lambda *_: ({"tool": "out_of_registry", "arguments": {}, "purpose": "measure"}, {})
        trace, _, reason = agent.investigate(backend, fake)
        self.assertEqual(reason, "decision_unavailable")
        backend.run.assert_called_once_with("sales", {})
        self.assertFalse(agent.valid_action({"tool": "sales", "arguments": {"extra": True}, "purpose": "measure"}, TOOLS))
        self.assertTrue(all(row.get("actor") != "qwen" for row in trace if row["step"] == "tool"))

    def test_source_text_stays_in_data_and_dummy_credential_is_redacted(self):
        marker = "Проверочная метка янтарь"
        source = entry("file:fixture", "interview", "Фикстура", marker + " password: fixture-only",
                       mode="synthetic", quality="excerpt")
        response = {"choices": [{"message": {"content": '{"hypotheses":[]}'}}]}
        opener = Mock(return_value=io.BytesIO(json.dumps(response).encode()))
        with patch.dict("os.environ", {"PM_LLM_URL": "http://127.0.0.1:18081/v1"}, clear=True):
            status, _, _, _ = qwen.infer("Что известно о покупке?", [source], opener=opener, planning=False)
        self.assertEqual(status, "ok")
        request = json.loads(opener.call_args.args[0].data)
        system, data = request["messages"]
        self.assertNotIn(marker, system["content"])
        self.assertIn(marker, data["content"])
        self.assertNotIn("fixture-only", data["content"])

    def test_malformed_and_unknown_references_fail_validation(self):
        with self.assertRaises(ValueError):
            qwen.parse('{"hypotheses":', {})
        with self.assertRaises(ValueError):
            qwen.parse('{"hypotheses":[{"cause":"Параметр мог потеряться",'
                       '"verify":"Сравнить два пути","expected":"Параметр появится",'
                       '"evidence_ids":["unknown"],"contradictions":[]}]}', {})

    def test_client_cannot_supply_private_history_or_expand_scope(self):
        engine = Mock()
        with self.assertRaisesRegex(ValueError, "область"):
            chat.consult({"question": "Проверь продажи", "scope": {"extra": []}}, analyze=engine)
        engine.assert_not_called()
        question, _, scope = chat.normalize_turn({"question": "Проверь продажи",
                                                   "history": [{"role": "user", "content": "another turn"}]})
        self.assertEqual(question, "Проверь продажи")
        self.assertEqual(scope, {})
