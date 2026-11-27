"""Estimate monthly infrastructure cost in RUB using the operator's own quote."""
import argparse
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path

# Unit prices are effective prices after allowances, in RUB per indicated unit.
# Enter zero explicitly when a resource is shared, unused or included in the quote.
UNITS = {"application_vm": "hour", "dedicated_model_vm": "hour",
         "block_disks": "GB_month", "versioned_backup": "GB_month",
         "backup_operations": "1000_requests", "public_ipv4": "hour",
         "billable_traffic": "GB", "source_api": "1000_requests"}
CENT = Decimal("0.01")


def number(value):
    result = Decimal(str(value))
    if not result.is_finite() or result < 0:
        raise ValueError
    return result


def template():
    return {"quote_date": None, "region": None, "managers": 5,
            "items": {name: {"unit": unit, "quantity": None, "rub_per_unit": None}
                      for name, unit in UNITS.items()}}


def estimate(data):
    if not all(isinstance(data.get(key), str) and data[key].strip() for key in ("region", "quote_date")):
        raise ValueError
    from datetime import date
    date.fromisoformat(data["quote_date"])
    managers = data["managers"]
    if type(managers) is not int or managers < 1 or set(data["items"]) != set(UNITS):
        raise ValueError
    lines = {}
    for name, unit in UNITS.items():
        item = data["items"][name]
        if item["unit"] != unit:
            raise ValueError
        lines[name] = number(item["quantity"]) * number(item["rub_per_unit"])
    total = sum(lines.values())
    return {"rub_by_resource": {k: str(v.quantize(CENT)) for k, v in lines.items()},
            "monthly_rub": str(total.quantize(CENT)), "rub_per_manager": str((total / managers).quantize(CENT))}


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    source = cli.add_mutually_exclusive_group(required=True)
    source.add_argument("--template", action="store_true")
    source.add_argument("--input", type=Path)
    args = cli.parse_args()
    try:
        result = template() if args.template else estimate(json.loads(args.input.read_text(encoding="utf8")))
    except (AttributeError, OSError, KeyError, InvalidOperation, TypeError, ValueError):
        cli.error("Provide a dated regional quote, a positive manager count and non-negative quantities and prices.")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
