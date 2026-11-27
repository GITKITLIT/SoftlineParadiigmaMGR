"""Run a repeatable analytical rubric against independent synthetic scenarios."""
import argparse
import json
import os
import sys
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pm.investigation import analyze
from pm.settings import ROOT


def fixture(scenario):
    data = json.loads((ROOT / "data/demo-overview.json").read_text(encoding="utf8"))
    if scenario == "stable":
        for point in data["series"][0]["points"]:
            point[1] = 100
    elif scenario == "sparse":
        data["series"] = data["series"][:1]
        after = data["config"]["after_start"]
        data["series"][0]["points"] = [point for point in data["series"][0]["points"] if point[0] != after]
    return data


def run(scenario, live):
    source = fixture(scenario)
    question = {
        "falling": "Почему упали заказы лицензий? Сопоставь контроль, релизы и бэклог.",
        "stable": "Есть ли снижение заказов? Проверь контрольную группу.",
        "sparse": "Оцени масштаб падения заказов и проверь наличие контроля.",
        "off_topic": "Как испечь хлеб без дрожжей?",
    }[scenario]
    report = analyze({"query": question}, snapshot_loader=lambda: deepcopy(source), persist=False)
    facts = [item for item in report["findings"] if item.get("statement_type") == "fact"]
    core = next((x for x in report["evidence"] if x["id"] == "orders:0"), None)
    reference_ids = {item["id"] for item in report["evidence"]}
    checks = {
        "citations": all(set(item["evidence_ids"] + item["contradictions"]) <= reference_ids
                         for item in report["findings"] + report["hypotheses"]),
        "typed_findings": all(item.get("statement_type") in {"fact", "assumption"} for item in report["findings"]),
        "actionable_checks": all(len(item["verification"].split()) >= 3 and bool(item.get("criteria"))
                                 for item in report["proposed_tasks"]),
    }
    if scenario == "falling":
        checks.update(
            numeric_effect=core is not None and core["values"]["delta"] is not None
                           and abs(core["values"]["delta"] + 30) < 0.001,
            control_contrast=any(row.get("relation") == "sales_control"
                                 and abs(row["values"]["difference_pp"] - (-30)) < 0.001 for row in facts),
            release_as_hypothesis=any(row.get("relation") == "sales_release"
                                      and row["statement_type"] == "assumption" for row in report["findings"]),
            proposed_predevelopment_check=bool(report["proposed_tasks"]),
        )
        if live:
            checks["live_hypothesis"] = report["llm_status"] == "ok" and bool(report["hypotheses"])
            checks["chose_tools"] = sum(row.get("tool") is not None and row.get("actor") == "qwen"
                                        for row in report["trace"]) >= 2
    elif scenario == "stable":
        checks["no_false_decline"] = core is not None and core["values"]["delta"] == 0 and "не достигло" in report["summary"]
        checks["no_false_task"] = all("паден" not in item["title"].casefold() for item in report["proposed_tasks"])
    elif scenario == "sparse":
        checks["no_invented_effect"] = core is not None and core["values"]["delta"] is None
        checks["no_control"] = not any(row.get("relation") == "sales_control" for row in report["findings"])
    else:
        checks["no_unrelated_acquisition"] = not report["evidence"] and report["termination"] == "out_of_scope"
    return dict(scenario=scenario, fixture="synthetic", llm_status=report["llm_status"],
                seconds=report["seconds"], usage=report["usage"], checks=checks,
                tools=[{"tool": row["tool"], "actor": row["actor"]} for row in report["trace"] if "tool" in row])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Use explicitly configured local Qwen")
    parser.add_argument("--scenario", action="append", choices=("falling", "stable", "sparse", "off_topic"))
    args = parser.parse_args()
    if args.live and not os.getenv("PM_LLM_URL"):
        parser.error("--live requires PM_LLM_URL")
    with TemporaryDirectory() as folder, patch.dict(os.environ, {
        "PM_PRIVATE_DIR": folder, "PM_WORKBOOK": "", "PM_SNAPSHOT": "", "PM_EVIDENCE": "",
        "PM_LLM_URL": os.getenv("PM_LLM_URL", "") if args.live else "", "PM_LLM_PROMPT_FILE": "",
        "METRIKA_TOKEN": "", "YANDEX_SEARCH_ENABLED": "false",
    }):
        reports = [run(case, args.live) for case in args.scenario or ("falling", "stable", "sparse", "off_topic")]
    print(json.dumps({"mode": "local_model" if args.live else "offline", "reports": reports}, ensure_ascii=False))
    return 0 if all(all(result["checks"].values()) for result in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())
