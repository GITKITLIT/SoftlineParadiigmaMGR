"""Measure the offline adapter with a fresh database and a warmed SQLite cache."""

import argparse
import json
import math
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from statistics import median
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pm.web_search import collect_competitor_evidence
from scripts.search_smoke import OFFLINE_INPUT, offline_fixture


def run(rounds, *, workers=8):
    cold = []
    warm = []
    for _ in range(rounds):
        with offline_fixture():
            cold_result = collect_competitor_evidence(**OFFLINE_INPUT)
            warm_result = collect_competitor_evidence(**OFFLINE_INPUT)
            if cold_result["status"] != "ok" or warm_result["status"] != "ok":
                raise RuntimeError("Синтетический сценарий не завершился")
            cold.append(cold_result)
            warm.append(warm_result)
    with offline_fixture(), ThreadPoolExecutor(max_workers=workers) as pool:
        load_started = perf_counter()
        load = list(pool.map(lambda _: collect_competitor_evidence(**OFFLINE_INPUT), range(rounds)))
        load_ms = round((perf_counter() - load_started) * 1000, 2)
    if any(entry["status"] != "ok" for entry in load):
        raise RuntimeError("Параллельный офлайн-сценарий не завершился")
    latencies = sorted(entry["metrics"]["elapsed_ms"] for entry in load)
    return {
        "mode": "offline",
        "rounds": rounds,
        "cold": {
            "median_ms": median(r["metrics"]["elapsed_ms"] for r in cold),
            "simulated_requests": sum(r["usage"]["requests"] for r in cold),
        },
        "warm": {
            "median_ms": median(r["metrics"]["elapsed_ms"] for r in warm),
            "simulated_requests": sum(r["usage"]["requests"] for r in warm),
            "cache_hit_rate": sum(r["usage"]["cache_hits"] for r in warm) / rounds,
        },
        "concurrent_shared_db": {
            "workers": workers, "calls": rounds, "wall_ms": load_ms,
            "p95_ms": latencies[math.ceil(0.95 * len(latencies)) - 1],
            "simulated_requests": sum(r["usage"]["requests"] for r in load),
            "cache_hits": sum(r["usage"]["cache_hits"] for r in load),
        },
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Офлайн-замер поиска и SQLite-кеша")
    parser.add_argument("--rounds", type=int, default=30)
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()
    if not 1 <= args.rounds <= 100 or not 1 <= args.workers <= 16:
        parser.error("Допустимо от 1 до 100 циклов и от 1 до 16 потоков")
    print(json.dumps(run(args.rounds, workers=args.workers), indent=2))
