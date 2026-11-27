import concurrent.futures
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app
from pm import ai_store, investigation
from pm.product_decisions import task_proposals
from pm.settings import ROOT


class DecisionTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ, {
            "PM_PRIVATE_DIR": str(self.folder), "PM_WORKBOOK": "", "PM_SNAPSHOT": "",
            "PM_EVIDENCE": "", "PM_LLM_URL": "", "YANDEX_SEARCH_ENABLED": "",
        }))

    def test_existing_task_moves_to_priority_review_with_explicit_criteria(self):
        row = {
            "problem": "Ошибка события корзины", "cause": "Не сохраняется событие",
            "verify": "Сопоставить показ события добавления в корзину с логом",
            "expected": "Видим подтверждение регистрации", "risk": "Неполный сбор",
            "evidence_ids": ["backlog:ref"], "contradictions": [], "alternative_explanations": ["Пустой сеанс"],
            "business_impact": "Потеря измеримости", "confidence": "low", "missing": "Нужен пример сеанса",
        }
        task = {"id": "SYNTH-30", "title": "Проверить событие добавления в корзину"}
        result = task_proposals([row], [task], [{"id": "backlog:ref", "kind": "backlog", "ref": task["id"]}],
                                None, {}, backlog_status="ok")[0]
        self.assertEqual(result["action"], "review_existing_priority")
        self.assertEqual(result["existing_tasks"], [task])
        self.assertIn(row["verify"], result["criteria"])

    def test_effect_handles_missing_control_and_explicit_stop_threshold(self):
        report = investigation.analyze({"query": "Проверим падение заказов"})
        proposal = report["proposed_tasks"][0]
        task = ai_store.decide(dict(run_id=report["run_id"], proposal_id=proposal["id"],
                                    target=200, stop_if_below=190, action="accept", idempotency_key="test"))
        obs = json.loads((ROOT / "data/effect-demo.json").read_text(encoding="utf8"))
        for row in obs["rows"]:
            row.pop("astra")
        value = ai_store.review({"task_id": task["id"], "mode": "simulation", "observation": obs})
        self.assertEqual(value["status"], "stop_condition_met")
        self.assertIsNone(value["control_change"])
        self.assertEqual(value["mode"], "simulation")
        self.assertEqual(ai_store.task_list()[0]["latest_review"], None)
        value = ai_store.review({"task_id": task["id"], "mode": "simulation",
                                 "observation": obs | {"external_factors": ["Синтетическая акция"]}})
        self.assertEqual(value["status"], "inconclusive")
        effect = app.api("/api/analyze", {"query": "Оцени эффект", "task_id": task["id"]})
        self.assertEqual(effect["sources_status"]["effect"]["status"], "partial")

    def test_reopened_analysis_differs_when_data_changes_and_daily_counts_runs(self):
        before = app.api("/api/analyze", {"query": "Динамика заказов"})
        data = json.loads((ROOT / "data/demo-overview.json").read_text(encoding="utf8"))
        week = data["config"]["after_start"]
        next(point for point in data["series"][0]["points"] if point[0] == week)[1] = 40
        path = self.folder / "synth-change.json"
        path.write_text(json.dumps(data), encoding="utf8")
        with patch.dict(os.environ, {"PM_SNAPSHOT": str(path)}):
            after = app.api("/api/analysis/reopen", {"run_id": before["run_id"]})
        self.assertEqual(after["previous_run_id"], before["run_id"])
        self.assertIn("orders:0", after["evidence_changes"]["updated"])
        daily = app.api("/api/analysis/daily", {})
        self.assertEqual(daily["groups"]["synthetic"]["runs"], 1)
        self.assertEqual(daily["groups"]["local"]["runs"], 1)

    def test_many_clients_share_one_idempotent_decision(self):
        report = investigation.analyze({"query": "Оценка падения заказов"})
        body = dict(run_id=report["run_id"], proposal_id=report["proposed_tasks"][0]["id"],
                    target=110, action="accept", idempotency_key="same")
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            values = list(pool.map(lambda _: ai_store.decide(body)["id"], range(16)))
        self.assertEqual(len(set(values)), 1)
        self.assertEqual(len(ai_store.task_list()), 1)

    def test_off_topic_request_never_reads_snapshot(self):
        def blocked():
            raise AssertionError("No sources may be used for this question")
        result = investigation.analyze({"query": "Как испечь хлеб?"}, snapshot_loader=blocked)
        self.assertEqual(result["llm_status"], "not_needed")
        self.assertEqual(result["evidence"], [])


    def test_positive_index_with_failed_side_effect_does_not_close_task(self):
        report = investigation.analyze({"query": "Падение заказов"})
        saved = ai_store.decide(dict(run_id=report["run_id"], proposal_id=report["proposed_tasks"][0]["id"],
                                     target=110, action="accept", guardrail_metrics={"retention": 95},
                                     idempotency_key="guardrails"))
        observation = json.loads((ROOT / "data/effect-demo.json").read_text(encoding="utf8"))
        baseline = ai_store.review({"task_id": saved["id"], "mode": "simulation", "observation": observation})
        self.assertEqual(baseline["status"], "inconclusive")
        self.assertEqual(baseline["side_effects"][0]["status"], "missing")
        observation["guardrails"] = {"retention": {"start": baseline["measurement"]["start"],
                                                   "end": baseline["measurement"]["end"],
                                                   "baseline": 99, "after": 92}}
        changed = ai_store.review({"task_id": saved["id"], "mode": "simulation", "observation": observation})
        self.assertEqual(changed["side_effects"][0]["status"], "below_minimum")
        self.assertEqual(changed["status"], "inconclusive")


    def test_missing_numeric_window_does_not_call_synthesis_for_a_measurement(self):
        data = json.loads((ROOT / "data/demo-overview.json").read_text(encoding="utf8"))
        start = data["config"]["after_start"]
        data["series"][0]["points"] = [point for point in data["series"][0]["points"] if point[0] != start]
        def inference(*args):
            raise AssertionError("Incomplete windows need a new observation, not model synthesis")
        report = investigation.analyze({"query": "Оцени масштаб падения заказов"},
                                       snapshot_loader=lambda: data, infer=inference)
        self.assertEqual(report["llm_status"], "not_needed")
        self.assertEqual(report["sources_status"]["sales"]["status"], "partial")
        self.assertFalse(report["hypotheses"])
