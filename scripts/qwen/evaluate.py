"""Run reproducible synthetic Qwen scenarios and emit aggregates only."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import perf_counter
from unittest.mock import patch
from urllib.request import ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from llm_lab.evaluation import scenarios
from pm import ai_store, brief, chat, investigation, monitor, qwen, service


def exercise(case, snapshot, earlier, task_id):
    question = scenarios.CASES[case]
    opts = {"snapshot_loader": lambda: scenarios.fixture_snapshot(case, snapshot)}
    request = {"query": question}
    if case in scenarios.STUB_SEARCH:
        request["web"] = scenarios.WEB_SCOPE
        opts["collector"] = scenarios.search_fixture(case)
    if case in {"contradiction", "metrika_unavailable"}:
        request["metrika"] = {"compare": True}
        opts["meter"] = scenarios.meter_for(case)
    if case in {"history", "product_switch"}:
        request["memory"] = scenarios.scope_for("Demo A" if case == "history" else "Demo B")
    if case == "failed_effect":
        request["task_id"] = task_id
    if case == "unequal_periods":
        request["config"] = {"after_end": "2025-03-24"}

    def analyze(body, **kwargs):
        return investigation.analyze(body, **opts, **kwargs)

    start = perf_counter()
    context_ok = True
    persistence_ok = True
    if case in scenarios.CHAT:
        prior = earlier if case in {"follow_up", "product_switch"} else None
        mode = "new_topic" if case == "product_switch" else "auto"
        turn = chat.consult({"question": question, "mode": mode,
                             "scope": {key: value for key, value in request.items() if key != "query"}},
                            previous=prior, analyze=analyze)
        report = turn["report"]
        if case in {"follow_up", "product_switch"}:
            expected = (earlier or {}).get("report", {}).get("run_id") if case == "follow_up" else None
            context_ok = turn["previous_run_id"] == expected
    elif case == "monitor":
        result = monitor.check({}, proactive=True, analyze=analyze)
        report = ai_store.run(result["run_id"])
    else:
        if case == "model_unavailable":
            with patch.dict(os.environ, {"PM_LLM_URL": "http://127.0.0.1:1/v1"}):
                report = analyze(request)
        else:
            report = analyze(request)
        if case == "release_brief":
            display = brief.release_brief(report["run_id"])
            persistence_ok = display["run_id"] == report["run_id"] and bool(display["measurements"])
    elapsed = perf_counter() - start
    passed = scenarios.checks(case, report)
    passed.update(context=context_ok, brief_persistence=persistence_ok)
    evidence = report["evidence"]
    return {
        "id": case, "wall_seconds": round(elapsed, 3), "analysis_seconds": report["seconds"],
        "llm_status": report["llm_status"], "termination": report["termination"],
        "model_tools": [step["tool"] for step in report["trace"] if step.get("tool") and step["actor"] == "qwen"],
        "hypotheses": len(report["hypotheses"]), "evidence_count": len(evidence),
        "source_groups": len({source.get("source_group", source["id"]) for source in evidence}),
        "usage": report["usage"], "checks": passed,
    }


def streaming_probe():
    # A synthetic endpoint probe. Core's structured requests remain non-streaming.
    url = qwen.endpoint()
    body = {
        "model": os.getenv("PM_LLM_MODEL", "qwen3-4b"), "max_tokens": 80,
        "temperature": 0, "stream": True, "stream_options": {"include_usage": True},
        "messages": [{"role": "user", "content": "Кратко объясни, зачем продакт-менеджеру контрольная группа."}],
    }
    req = Request(url, json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    started = perf_counter()
    first = None
    usage = {}
    opener = build_opener(ProxyHandler({}), qwen.NoRedirect()).open
    with opener(req, timeout=30) as response:
        for line in response:
            if not line.startswith(b"data: ") or line.strip() == b"data: [DONE]":
                continue
            chunk = json.loads(line[6:])
            usage.update(qwen.token_usage(chunk))
            if first is None and any(item.get("delta", {}).get("content") for item in chunk.get("choices", [])):
                first = perf_counter()
    ended = perf_counter()
    return {
        "ttft_seconds": round(first - started, 3) if first else None,
        "wall_seconds": round(ended - started, 3), "usage": usage,
        "tokens_per_second": round((usage["completion_tokens"] - 1) / (ended - first), 2)
        if first and ended > first and usage.get("completion_tokens", 0) > 1 else None,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("fallback", "live"), required=True)
    parser.add_argument("--case", action="append", choices=scenarios.CASES)
    parser.add_argument("--concurrent", action="store_true")
    parser.add_argument("--server-pid", type=int)
    args = parser.parse_args()
    cases = args.case or list(scenarios.CASES)
    with TemporaryDirectory(prefix="qwen-eval-") as state, patch.dict(os.environ, {
        "PM_PRIVATE_DIR": state, "PM_WORKBOOK": "", "PM_SNAPSHOT": "", "PM_CASE_CONFIG": "",
        "PM_EVIDENCE": "", "PM_DB_MAX_MIB": "", "PM_MIGRATION_STATUS_FILE": "",
        "METRIKA_TOKEN": "", "METRIKA_COUNTER_ID": "", **scenarios.WEB_ENV,
    } | ({"PM_LLM_URL": ""} if args.mode == "fallback" else {})):
        if args.mode == "live" and not qwen.endpoint():
            parser.error("Set PM_LLM_URL to an approved loopback llama-server")
        snapshot = service.overview()
        scenarios.seed_history()
        ai_store.store_excerpts("qwen-eval-old-competitor", [{
            "id": "competitor-old", "kind": "Конкуренты", "ref": "Синтетическое конкурентное исследование",
            "text": "Старое исследование сравнило покупку лицензии. Выбор подписки происходил в каталоге.",
            "observed_at": "2022-01-01T00:00:00Z", "data_mode": "synthetic",
        }])
        task = scenarios.effect_task() if "failed_effect" in cases else None
        earlier = None
        if any(case in cases for case in ("follow_up", "product_switch")):
            earlier = chat.consult({"question": "Почему упали заказы Demo A? Проверь воронку."})
        samples = [exercise(case, snapshot, earlier, task) for case in cases]
        result = {"mode": args.mode, "data": "synthetic", "samples": samples}
        if args.concurrent:
            start = perf_counter()
            with ThreadPoolExecutor(max_workers=2) as clients:
                pair = list(clients.map(lambda case: exercise(case, snapshot, None, None),
                                        ("simple_chat", "draft")))
            result["concurrent"] = {"clients": 2, "wall_seconds": round(perf_counter() - start, 3), "samples": pair}
        if args.mode == "live":
            result["streaming_endpoint"] = streaming_probe()
        if args.server_pid is not None:
            rss = subprocess.check_output(["ps", "-o", "rss=", "-p", str(args.server_pid)], text=True)
            result["server_rss_mib"] = round(int(rss.strip()) / 1024, 1)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return int(any(not all(row["checks"].values()) for row in samples))


if __name__ == "__main__":
    raise SystemExit(main())
