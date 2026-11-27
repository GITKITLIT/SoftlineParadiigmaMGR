"""Estimate monthly infrastructure cost in RUB using the operator's own quote."""
import argparse
import json
import sys
from math import isfinite
from decimal import Decimal, InvalidOperation
from pathlib import Path

# Unit prices are effective prices after allowances, in RUB per indicated unit.
# Enter zero explicitly when a resource is shared, unused or included in the quote.
UNITS = {"application_vm": "hour", "dedicated_model_vm": "hour",
         "block_disks": "GB_month", "versioned_backup": "GB_month",
         "backup_operations": "1000_requests", "public_ipv4": "hour",
         "billable_traffic": "GB", "source_api": "1000_requests"}
OPTIONAL_UNITS = {"smtp": "1000_messages"}
COHORTS = (1, 5, 35)
CENT = Decimal("0.01")


def number(value):
    result = Decimal(str(value))
    if not result.is_finite() or result < 0:
        raise ValueError
    return result


def template():
    return {"quote_date": None, "region": None, "managers": 5,
            "items": {name: {"unit": unit, "quantity": None, "rub_per_unit": None}
                      for name, unit in (UNITS | OPTIONAL_UNITS).items()}}


def estimate(data):
    if not all(isinstance(data.get(key), str) and data[key].strip() for key in ("region", "quote_date")):
        raise ValueError
    from datetime import date
    date.fromisoformat(data["quote_date"])
    managers = data["managers"]
    configured = data["items"]
    if (type(managers) is not int or managers < 1
            or not set(UNITS) <= set(configured) <= set(UNITS | OPTIONAL_UNITS)):
        raise ValueError
    lines = {}
    for name, item in configured.items():
        if item["unit"] != (UNITS | OPTIONAL_UNITS)[name]:
            raise ValueError
        lines[name] = number(item["quantity"]) * number(item["rub_per_unit"])
    total = sum(lines.values())
    return {"rub_by_resource": {k: str(v.quantize(CENT)) for k, v in lines.items()},
            "monthly_rub": str(total.quantize(CENT)), "rub_per_manager": str((total / managers).quantize(CENT))}


def with_qwen(data, benchmark, *, monitor_runs=None, model_hour=None):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from scripts.qwen.cost import MONITOR_RUNS, PER_MANAGER, estimates

    infrastructure = estimate(data)
    model = data["items"]["dedicated_model_vm"]
    provider = model if number(model["quantity"]) else data["items"]["application_vm"]
    price = float(number(provider["rub_per_unit"] if model_hour is None else model_hour))
    monitor_runs = MONITOR_RUNS if monitor_runs is None else monitor_runs
    if not isfinite(price) or monitor_runs < 0:
        raise ValueError
    workload = estimates(benchmark, PER_MANAGER, monitor_runs, COHORTS, price)
    return {"fixed_and_usage_quote_month": infrastructure, "model_compute_estimate_week": workload,
            "quote_region": data["region"], "quote_date": data["quote_date"], "quote_managers": data["managers"]}


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    source = cli.add_mutually_exclusive_group(required=True)
    source.add_argument("--template", action="store_true")
    source.add_argument("--input", type=Path)
    cli.add_argument("--qwen-benchmark", type=Path, help="Add workload estimates from a live benchmark")
    cli.add_argument("--model-hour-rub", help="Override the model reference machine-hour rate")
    cli.add_argument("--monitor-runs", type=int, help="Weekly triggered investigations per deployment")
    args = cli.parse_args()
    if (args.qwen_benchmark and not args.input
            or not args.qwen_benchmark and (args.model_hour_rub is not None or args.monitor_runs is not None)):
        cli.error("Provide --input and --qwen-benchmark together for a combined estimate.")
    try:
        if args.template:
            result = template()
        else:
            data = json.loads(args.input.read_text(encoding="utf-8-sig"))
            if args.qwen_benchmark:
                benchmark = json.loads(args.qwen_benchmark.read_text(encoding="utf-8-sig"))
                result = with_qwen(data, benchmark, monitor_runs=args.monitor_runs, model_hour=args.model_hour_rub)
            else:
                result = estimate(data)
    except (AttributeError, OSError, KeyError, InvalidOperation, TypeError, ValueError):
        cli.error("Provide a dated regional quote, a positive manager count and non-negative quantities and prices.")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
