"""Verify a private release report against primary workbook rows with no inference."""

import argparse
from datetime import date, timedelta
import json
from math import isclose
from pathlib import Path
from statistics import mean

from openpyxl import load_workbook


def rows(workbook, name):
    data = iter(workbook[name].values)
    columns = next(data)
    return [dict(zip(columns, row)) for row in data if any(value is not None for value in row)]


def day(value):
    return date.fromisoformat(str(value)[:10])


def weeks(start, end):
    cursor = day(start)
    while cursor <= day(end):
        yield cursor.isoformat()
        cursor += timedelta(days=7)


def verify(workbook, case, report):
    evidence = {item["id"]: item for item in report["evidence"]}
    request = report.get("request_context", {})
    cfg = case["periods"] | request.get("config", {})
    metric = request.get("config", {}).get("metric", "orders_index")
    releases = {row["release_id"]: row for row in rows(workbook, "Релизы")}
    release = releases[case["release_id"]]
    sales = rows(workbook, "Продажи")
    traffic = rows(workbook, "Метрики_данные")
    launch = day(release["release_date"])
    gate = day(cfg["before_end"]) + timedelta(days=7) <= launch < day(cfg["after_start"])
    gate &= day(cfg["after_end"]) < day(cfg["partial_week"])
    tests = {"workbook_mode": report["data_mode"] == "local", "windows_exclude_launch_and_partial": gate}
    detail = {"orders": {}, "funnel": {}}
    for index, family in enumerate(case["families"]):
        family_rows = {str(row["week_start"])[:10]: row for row in sales
                       if row["product_family"] == family and row["channel"] == case["channel"]}
        averages = [mean(family_rows[date][metric] for date in weeks(cfg[f"{which}_start"], cfg[f"{which}_end"]))
                    for which in ("before", "after")]
        before, after = averages
        delta = 100 * (after - before) / before
        ref = evidence[f"orders:{index}"]
        tests[f"orders_{index}"] = (ref["ref"] == family and
                                   isclose(ref["values"]["before"], before) and
                                   isclose(ref["values"]["after"], after) and
                                   isclose(ref["values"]["delta"], delta))
        detail["orders"][family] = dict(before=before, after=after, change_percent=delta)
    for segment in ("desktop", "mobile"):
        selected = [row for row in traffic if row["product"] == case["reach_product"] and row["segment"] == segment
                    and day(cfg["reach_start"]) <= day(row["date"]) <= day(cfg["reach_end"])]
        reach = {day(row["date"]): json.loads(row["detail_json"]) for row in selected
                 if row["metric_id"] == "cfg_reach_rate" and row["detail_json"]}
        num = sum(row["configurator_visits"] for row in reach.values())
        den = sum(row["family_visits"] for row in reach.values())
        coverage = 1 + (day(cfg["reach_end"]) - day(cfg["reach_start"])).days
        rate = num * 100 / den if den else None
        actual = evidence[f"reach:{segment}"]["values"]
        tests[f"reach_{segment}"] = (actual["num"], actual["den"], actual["days"], actual["total"]) == (
            num, den, len(reach), coverage,
        ) and (isclose(actual["rate"], rate) if rate is not None else actual["rate"] is None)
        cart = {day(row["date"]) for row in selected if row["metric_id"] == "cfg_add_to_cart_rate"
                and row["value"] is not None}
        basket = evidence.get(f"basket:{segment}", {}).get("values")
        tests[f"cart_coverage_{segment}"] = bool(basket and basket["days"] == len(cart) and basket["rate"] is None)
        detail["funnel"][segment] = {"num": num, "den": den, "rate": rate, "days": len(reach),
                                     "total": coverage, "cart_observed_days": len(cart)}
    tests["linked_release_task"] = (
        evidence.get("release:current", {}).get("values", {}).get("related_task") == release["related_task_ids"]
    )
    tests["separate_backlog_task"] = (
        evidence.get("backlog:related", {}).get("ref") == case["backlog_id"]
    )
    tests["resolved_references"] = all(set(item["evidence_ids"] + item["contradictions"]) <= evidence.keys()
                                       for item in report["hypotheses"] + report["findings"])
    return tests, detail


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workbook", required=True, type=Path)
    parser.add_argument("--case", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--private-numbers", action="store_true")
    args = parser.parse_args()
    with args.workbook.open("rb") as stream:
        workbook = load_workbook(stream, data_only=True, read_only=True)
        try:
            checks, detail = verify(workbook, json.loads(args.case.read_text(encoding="utf-8-sig")),
                                    json.loads(args.report.read_text(encoding="utf-8-sig")))
        finally:
            workbook.close()
    result = {"checks": checks} | ({"private_calculations": detail} if args.private_numbers else {})
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return int(not all(checks.values()))


if __name__ == "__main__":
    raise SystemExit(main())
