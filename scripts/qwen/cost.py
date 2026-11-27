"""Estimate scenario workload using measured slot time and a supplied machine-hour price."""

import argparse
from collections import defaultdict
import json
from math import isfinite
from pathlib import Path
from statistics import mean

# Illustrative weekly usage per active manager. Monitor runs are per deployment.
PER_MANAGER = {"simple_chat": 15, "follow_up": 10, "release_brief": 2, "drop": 2,
               "history": 2, "incomparable_offers": 1, "draft": 2}
MONITOR_RUNS = 7


def estimates(benchmark, rates, monitor_runs, cohorts, price):
    if benchmark["mode"] != "live":
        raise ValueError("Use timings from a live benchmark")
    timings = defaultdict(list)
    for sample in benchmark["samples"]:
        timings[sample["id"]].append(sample["wall_seconds"])
    missing = [case for case in [*rates, "monitor"] if not timings[case]]
    if missing:
        raise ValueError("Missing timings: " + ", ".join(missing))
    per_user = sum(count * mean(timings[case]) for case, count in rates.items())
    fixed = monitor_runs * mean(timings["monitor"])
    rows = []
    for size in cohorts:
        hours = (size * per_user + fixed) / 3600
        rows.append({"managers": size, "weekly_compute_equivalent_hours": round(hours, 4),
                     "weekly_compute_estimate": round(hours * price, 4),
                     "weekly_dedicated_host_estimate": round(168 * price, 4)})
    return {"machine_hour_price": price, "assumed_weekly_requests_per_manager": rates,
            "assumed_weekly_triggered_investigations": monitor_runs,
            "mean_seconds_per_case": {case: mean(timings[case]) for case in [*rates, "monitor"]},
            "estimates": rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("benchmark", type=Path)
    parser.add_argument("--machine-hour", required=True, type=float)
    parser.add_argument("--manager-count", type=int, action="append")
    parser.add_argument("--monitor-runs", type=int, default=MONITOR_RUNS)
    args = parser.parse_args()
    cohorts = args.manager_count or [1, 5, 35]
    if not isfinite(args.machine_hour) or args.machine_hour < 0 or min(cohorts) < 1 or args.monitor_runs < 0:
        parser.error("Use a nonnegative price and run count, and positive manager counts")
    benchmark = json.loads(args.benchmark.read_text(encoding="utf8"))
    print(json.dumps(estimates(benchmark, PER_MANAGER, args.monitor_runs, cohorts, args.machine_hour),
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
