"""Measure HTTP integration without printing evidence, queries or model responses."""
import argparse
import json
import math
import statistics
from time import perf_counter
from urllib.request import Request, urlopen

p = argparse.ArgumentParser(description=__doc__)
p.add_argument("--port", type=int, default=8080)
p.add_argument("--runs", type=int, choices=range(1, 21), default=1, metavar="1..20")
p.add_argument("--mode", choices=("fallback", "stub", "real"), default="fallback")
args = p.parse_args()

times, tokens, statuses = [], [], []
reference_failures = 0
for question in ("Сигнал заказов", "Корзина и события", "Контрольная группа") * 7:
    if len(times) == args.runs:
        break
    req = Request(f"http://127.0.0.1:{args.port}/api/analyze",
                  data=json.dumps({"query": question}).encode(),
                  headers={"Content-Type": "application/json"})
    started = perf_counter()
    with urlopen(req, timeout=160) as response:
        report = json.load(response)
    times.append(perf_counter() - started)
    statuses.append(report["llm_status"])
    ids = {item["id"] for item in report["evidence"]}
    for item in report["findings"] + report["hypotheses"]:
        if not set(item["evidence_ids"] + item["contradictions"]) <= ids:
            reference_failures += 1
    if "total_tokens" in report["usage"]:
        tokens.append(report["usage"]["total_tokens"])
expected = "unavailable" if args.mode == "fallback" else "ok"
out = dict(mode=args.mode, runs=len(times), statuses=statuses, reference_failures=reference_failures,
           median_seconds=round(statistics.median(times), 3),
           p95_seconds=round(sorted(times)[math.ceil(.95 * len(times)) - 1], 3) if len(times) >= 10 else None,
           total_tokens_by_run=tokens)
print(json.dumps(out))
if reference_failures or any(status != expected for status in statuses):
    raise SystemExit(1)
