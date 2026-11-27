"""Exercise the opt-in Sandy Bridge Core profile against a ready loopback endpoint."""
import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from threading import Barrier, local
from time import monotonic
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pm import qwen
from scripts.qwen import production_benchmark as bench

CASES = ("history", "drop", "concurrent")
elapsed = local()
origin = monotonic()

def measure(*handlers):
    opener = bench.instrument(*handlers)
    recorded = opener.open

    @contextmanager
    def open_with_offset(request, *args, **kwargs):
        began = monotonic() - origin
        if elapsed.first is None:
            elapsed.first = began
        try:
            with recorded(request, *args, **kwargs) as response:
                yield response
        finally:
            elapsed.last = monotonic() - origin

    opener.open = open_with_offset
    return opener


def observe(run):
    elapsed.first = elapsed.last = None
    arrived = monotonic() - origin
    result = run()
    return result | {"arrived_seconds": round(arrived, 4),
                     "first_http_seconds": round(elapsed.first, 4) if elapsed.first is not None else None,
                     "last_http_seconds": round(elapsed.last, 4) if elapsed.last is not None else None,
                     "time_to_first_http_seconds": round(elapsed.first - arrived, 4)
                     if elapsed.first is not None else None,
                     "completed_seconds": round(monotonic() - origin, 4)}


def pair():
    with bench.isolated_state():
        snapshot = bench.service.overview()
        gate = Barrier(2)

        def client(case):
            gate.wait()
            return observe(lambda: bench.with_calls(lambda: bench.synthetic(case, snapshot)))

        with ThreadPoolExecutor(max_workers=2) as workers:
            clients = list(workers.map(client, ("simple_chat", "draft")))
        first, second = sorted(clients, key=lambda item: item["first_http_seconds"]
                              if item["first_http_seconds"] is not None else float("inf"))
        sequential = first["last_http_seconds"] is not None and second["first_http_seconds"] is not None and (
            first["last_http_seconds"] <= second["first_http_seconds"])
        return {"case": "serialized_pair", "clients": clients, "sequential_http": sequential,
                "submitted_together": abs(first["arrived_seconds"] - second["arrived_seconds"]) < 1,
                "wall_seconds": round(max(c["completed_seconds"] for c in clients)
                                      - min(c["arrived_seconds"] for c in clients), 4)}


def passes(result):
    calls = result["model_http"]
    return (bool(calls) and all(result["checks"].values()) and
            all(call["result"] == "stop" and not call.get("truncated") for call in calls))


def emit(result):
    print(json.dumps(result), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18081)
    parser.add_argument("--profile", choices=("recorded", "candidate"), default="candidate")
    parser.add_argument("--case", action="append", choices=CASES, help="Run only the selected cases")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("The loopback port must be between 1 and 65535")
    cases = tuple(dict.fromkeys(args.case)) if args.case else (CASES if args.profile == "candidate" else CASES[1:])
    context, slots = bench.model_properties(args.port)
    if (context, slots) != (8192, 1):
        parser.error(f"Expected one 8192-token slot. Endpoint reported {slots} slots and context {context}")
    budget = qwen.SANDY_BRIDGE_BUDGET
    if args.profile == "candidate":
        budget = replace(budget, planning_step=120, planning=410, synthesis_attempt=240, synthesis=270, total=700)
    env = {
        "PM_LLM_URL": f"http://127.0.0.1:{args.port}/v1",
        "PM_QWEN_RUNTIME_PROFILE": "sandy-bridge-one-slot",
        "PM_QWEN_STABLE_PLANNER_PREFIX": "true" if args.profile == "candidate" else "false",
    }
    emit({"event": "profile", "started_utc": datetime.now(timezone.utc).isoformat(),
          "profile": args.profile, "context": context, "slots": slots, "cases": cases,
          "stable_planner_prefix": env["PM_QWEN_STABLE_PLANNER_PREFIX"] == "true", "budget_seconds": asdict(budget)})

    results = []
    with patch.dict(os.environ, env), patch.object(qwen, "build_opener", measure), patch.object(
        qwen, "SANDY_BRIDGE_BUDGET", budget
    ):
        for case in cases:
            if case == "concurrent":
                result = pair()
                passed = result["sequential_http"] and result["submitted_together"] and all(
                    passes(client) for client in result["clients"]
                )
            else:
                result = observe(lambda: bench.run_case(case, None))
                if case == "history":
                    result["checks"]["historical_tools"] = {"compare_history", "search_history"} <= set(result["model_tools"])
                passed = passes(result)
            results.append(passed)
            emit(result | {"passed": passed, "finished_utc": datetime.now(timezone.utc).isoformat()})
    return 0 if all(results) else 2


if __name__ == "__main__":
    raise SystemExit(main())
