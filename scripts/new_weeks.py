"""Export weekly aggregates from a new v17 workbook for the effect panel."""

import argparse
import json
import sys
from pathlib import Path
from datetime import timedelta
from openpyxl import load_workbook

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pm.analytics import day, records, finite
from pm.settings import private_dir, workbook_case

p = argparse.ArgumentParser()
p.add_argument("workbook", type=Path)
p.add_argument("--fix", required=True)
p.add_argument("--complete-through", required=True)
p.add_argument("--output", type=Path, default=private_dir() / "new-weeks.json")
args = p.parse_args()
case = workbook_case()
families = case["families"]
fix, cut = day(args.fix), day(args.complete_through)
w = load_workbook(args.workbook, read_only=True, data_only=True)
weekly = {}
try:
    for r in records(w["Продажи"]):
        d = day(r["week_start"])
        fam = r["product_family"]
        if (
            r["channel"] != case["channel"]
            or fam not in families
            or not fix - timedelta(weeks=6) <= d <= cut
        ):
            continue
        row = weekly.setdefault(
            d, dict(week=d.isoformat(), complete=d + timedelta(days=6) <= cut)
        )
        k = ("kaspersky", "astra")[families.index(fam)]
        if k in row:
            raise ValueError(f"Дублируется {d} для {fam}")
        row[k] = finite(r["orders_index"])
finally:
    w.close()
rows = [r for _, r in sorted(weekly.items()) if "kaspersky" in r and "astra" in r]
args.output.parent.mkdir(parents=True, exist_ok=True)
args.output.write_text(
    json.dumps(dict(fix=fix.isoformat(), rows=rows), ensure_ascii=False, indent=2),
    encoding="utf8",
)
print(args.output)
