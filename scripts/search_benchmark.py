"""Measure the offline adapter with a fresh database and a warmed SQLite cache."""

import argparse
import json
import sys
from pathlib import Path
from statistics import median

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pm.web_search import collect_competitor_evidence
from scripts.search_smoke import OFFLINE_INPUT, offline_fixture


def run(rounds):
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
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Офлайн-замер поиска и SQLite-кеша")
    parser.add_argument("--rounds", type=int, default=30)
    args = parser.parse_args()
    if not 1 <= args.rounds <= 100:
        parser.error("--rounds должен быть от 1 до 100")
    print(json.dumps(run(args.rounds), indent=2))
