"""Export an investigation or approved Redmine payload to a private local file."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pm import ai_store
from pm.settings import ROOT

p = argparse.ArgumentParser(description=__doc__)
p.add_argument("kind", choices=("report", "redmine", "public-summary"))
p.add_argument("identifier")
p.add_argument("output", type=Path)
args = p.parse_args()
path = args.output.resolve()
if args.kind != "public-summary" and path.is_relative_to(ROOT):
    p.error("Private exports must go outside the repository.")
report = {"report": ai_store.run, "redmine": ai_store.redmine_export,
          "public-summary": ai_store.public_report}[args.kind](args.identifier)
with path.open("x", encoding="utf8") as stream:
    json.dump(report, stream, ensure_ascii=False, indent=2)
