"""Verify plan reuse and measured effect through the patched AI Core chat."""

import argparse
from datetime import date, timedelta
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from llm_lab.evaluation.scenarios import effect_task
from llm_lab.integration import chat_api
from pm import ai_store, chat, qwen
from scripts.qwen.historical_smoke import seed


def lookup(label, body, tool, *, live):
    turn = chat.consult(body)
    report = turn["report"]
    assert report["llm_status"] == "not_needed", label
    names = [row for row in report["trace"] if row["step"] == "tool"]
    assert [row["tool"] for row in names] == [tool], label
    planner_tokens = report["usage"].get("prompt_tokens", 0)
    if live and tool == "sales":
        assert planner_tokens > 0, label
    if tool == "effect":
        assert names[0]["actor"] == "server" and planner_tokens == 0, label
    return turn, {"case": label, "tool": tool, "actor": names[0]["actor"], "outcome": names[0]["status"],
                  "planner_prompt_tokens": planner_tokens, "llm_status": report["llm_status"]}


def check_cases(*, live):
    cases = []
    stored = seed()
    with patch.dict(os.environ, {"PM_LLM_URL": ""}):
        waiting_id = effect_task()
        draft = chat.consult({"question": "Почему упали заказы? Проверь воронку."})
    first = draft["report"]["proposed_tasks"][0]
    assert "изменение среднего индекса" in first["problem_description"]
    approval = {"proposal_id": first["id"], "confirm": True, "target": 130,
                "draft": {"problem_description": "Есть пропуски наблюдений.",
                          "verification": "Сверить журнал с фактическим шагом."}}
    task = chat_api.approve(draft, approval)
    assert chat_api.approve(draft, approval)["id"] == task["id"]
    assert task["problem_description"] == approval["draft"]["problem_description"]
    assert task["criteria"][0] == task["verification"]
    assert ai_store.accepted_task(task["id"])["tag"] == ai_store.TASK_TAG
    cases.append({"case": "draft_saved_once", "outcome": "ok"})

    plan, run = lookup("fresh_plan", {"question": "Как измерить эффект?"}, "sales", live=live)
    view = plan["reply"]
    assert view["effect_plan"]["baseline"] == 100 and view["effect_plan"]["target"] is None
    assert view["effect_plan"]["observation_weeks"] == 4 and view["expand_effect_plan"]
    assert "с базой 100" in view["answer"]
    cases.append(run)

    analyzer = Mock(side_effect=AssertionError("Plan should be read from the previous turn"))
    reused = chat.consult({"question": "Как измерить эффект?"}, previous=draft, analyze=analyzer)
    assert reused["reply"]["effect_plan"] == draft["reply"]["effect_plan"]
    assert not reused["reply"]["usage"] and not reused["reply"]["trace"]
    analyzer.assert_not_called()
    cases.append({"case": "previous_plan_without_inference", "outcome": "ok", "planner_prompt_tokens": 0})

    reviewed_id = next(row["id"] for row in ai_store.task_list() if row["run_id"] == stored["reviewed"])
    for label, task_id in (("waiting", waiting_id), ("measured", reviewed_id), ("missing", "unknown-task")):
        turn, run = lookup(label, {
            "question": "Какой эффект показала согласованная задача?", "scope": {"task_id": task_id},
        }, "effect", live=live)
        view = turn["reply"]
        if label == "waiting":
            assert "Ожидаются четыре полные недели" in view["answer"]
            assert view["next_step"] == "Дождаться четырёх полных недель."
            assert view["data_mode"] == "synthetic" and view["effect_plan"]["target"] == 125
            assert "kaspersky" not in view["sources"][0]["values"]
        elif label == "measured":
            values = view["sources"][0]["values"]
            assert "Цель не достигнута" in view["answer"] and "80 при цели 130" in view["answer"]
            assert view["next_step"] == "Пересмотреть гипотезу"
            assert values["start"] == "2025-04-14" and values["end"] == "2025-05-11"
            assert values["control_adjusted_change_pp"] is not None
        else:
            assert "оценка эффекта недоступна" in view["answer"]
            assert not view["measurements"] and not view["next_step"]
        cases.append(run)
    fix = date(2025, 4, 21)
    actual = ai_store.review({"task_id": reviewed_id, "mode": "actual", "observation": {
        "fix": fix.isoformat(), "rows": [{"week": (fix + timedelta(weeks=i)).isoformat(),
                                         "complete": True, "kaspersky": 80, "astra": 95} for i in range(3)],
    }})
    assert actual["status"] == "waiting_for_data"
    turn, run = lookup("stored_incomplete", {"question": "Какой эффект показала согласованная задача?",
                                             "scope": {"task_id": reviewed_id}}, "effect", live=live)
    assert run["outcome"] == "partial"
    assert "Ожидаются четыре полные недели" in turn["reply"]["answer"]
    assert turn["reply"]["sources"][0]["quality"] == "incomplete"
    cases.append(run)
    return {"data": "synthetic", "checks": cases}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    with TemporaryDirectory(prefix="qwen-effect-smoke-") as state, patch.dict(os.environ, {
        "PM_PRIVATE_DIR": state, "PM_WORKBOOK": "", "PM_SNAPSHOT": "", "PM_CASE_CONFIG": "",
        "PM_EVIDENCE": "", "YANDEX_SEARCH_ENABLED": "false", "PM_EMAIL_ENABLED": "false",
    } | ({} if args.live else {"PM_LLM_URL": ""})):
        if args.live and not qwen.endpoint():
            parser.error("Set PM_LLM_URL to the local llama-server")
        print(json.dumps(check_cases(live=args.live), indent=2))


if __name__ == "__main__":
    main()
