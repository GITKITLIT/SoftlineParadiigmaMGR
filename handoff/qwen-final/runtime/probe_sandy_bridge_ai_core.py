"""Exercise the opt-in Sandy Bridge Core profile against the Linux loopback."""
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from threading import Barrier, local
from time import monotonic
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pm import qwen
from scripts.qwen import production_benchmark as bench

assert bench.model_properties(18081) == (8192, 1)
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
    return all(result["checks"].values()) and all(
        call["result"] == "stop" for call in result["model_http"]
    )


with patch.dict(os.environ, {"PM_LLM_URL": "http://127.0.0.1:18081/v1",
                             "PM_QWEN_RUNTIME_PROFILE": "sandy-bridge-one-slot"}), patch.object(
                             qwen, "build_opener", measure):
    print(json.dumps({"budget_seconds": asdict(qwen.SANDY_BRIDGE_BUDGET)}), flush=True)
    drop = observe(lambda: bench.run_case("drop", None))
    print(json.dumps(drop), flush=True)
    clients = pair()
    print(json.dumps(clients), flush=True)
raise SystemExit(0 if passes(drop) and clients["sequential_http"] and clients["submitted_together"]
                 and all(passes(c) for c in clients["clients"]) else 2)
