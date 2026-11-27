import os
import unittest
from unittest.mock import Mock, patch

from pm.agent import MAX_MODEL_STEPS, investigate
from pm.analysis_context import InvestigationContext
from pm.qwen import bounded_evidence


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {"YANDEX_SEARCH_ENABLED": "", "PM_WORKBOOK": "", "PM_SNAPSHOT": ""}))

    def test_finish_out_of_scope_uses_no_sources(self):
        snapshot_loader = Mock(side_effect=AssertionError("No data should be acquired"))
        context = InvestigationContext("Как испечь хлеб?", {}, snapshot_loader=snapshot_loader)
        decide = Mock(return_value=({"tool": "finish", "arguments": {}, "purpose": "out_of_scope"}, {}))
        trace, usage, termination = investigate(context, decide)
        self.assertEqual(termination, "out_of_scope")
        self.assertFalse(context.evidence)
        self.assertEqual([row["step"] for row in trace], ["decision"])
        snapshot_loader.assert_not_called()

    def test_wrong_arguments_cannot_leak_to_an_external_adapter(self):
        # Local fallback after an invalid decision must not trigger the unselected network tool.
        meter = Mock(side_effect=AssertionError("No network call is authorized by this model action"))
        with patch("pm.analysis_context.sources", return_value=[]):
            context = InvestigationContext("Проверь динамику", {"metrika": {"start": "2025-01-01", "end": "2025-01-01"}},
                                           meter=meter)
            bad = {"tool": "metrika", "arguments": {"url": "https://example.com"}, "purpose": "measure"}
            trace, _, termination = investigate(context, Mock(return_value=(bad, {})))
        self.assertEqual(termination, "decision_unavailable")
        self.assertEqual({x["actor"] for x in trace if x["step"] == "tool"}, {"server"})
        self.assertIn("orders:0", context.evidence)
        meter.assert_not_called()

    def test_tools_get_fresh_results_and_repeat_is_rejected(self):
        metrika = Mock(return_value={"status": "ok", "scope": "demo_counter", "period": {},
                                     "metrics": {"visits": {"unit": "visits", "metric": "ym:s:visits", "value": 25}}})
        context = InvestigationContext("Визиты и заказы", {"metrika": {"start": "2025-01-01", "end": "2025-01-02"}},
                                       meter=metrika)
        seen = []
        def decide(query, remaining, evidence, trace, deadline):
            seen.append((set(remaining), {x["id"] for x in evidence}))
            tool = "metrika" if len(seen) in (1, 3) else "sales"
            return {"tool": tool, "arguments": {}, "purpose": "compare"}, {"total_tokens": 10}
        with patch("pm.analysis_context.sources", return_value=[]):
            _, usage, termination = investigate(context, decide)
        self.assertEqual(seen[0][1], set())
        self.assertEqual(seen[1][1], {"metrika:visits"})
        self.assertNotIn("metrika", seen[2][0])
        self.assertIn("orders:1", seen[2][1])
        self.assertEqual(termination, "decision_unavailable")
        self.assertEqual(usage["total_tokens"], 30)
        metrika.assert_called_once_with("2025-01-01", "2025-01-02", segment=None, detailed=True)

    def test_context_budget_preserves_distinct_single_sources(self):
        items = [{"id": f"interview:{i}", "kind": "interview", "text": "а" * 700} for i in range(20)]
        items += [{"id": "orders:0", "kind": "orders", "values": {"delta": -12.7}},
                  {"id": "release:0", "kind": "release", "text": "Опция доставки"}]
        packed = bounded_evidence(items, 1200, 250)
        self.assertEqual({item["kind"] for item in packed}, {"interview", "orders", "release"})
        self.assertEqual(next(x for x in packed if x["kind"] == "orders")["values"]["delta"], -12.7)
