"""Save a full local workbook investigation to a private file for acceptance."""

import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from pm import ai_store, investigation, qwen
from pm.settings import private_dir


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--question", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    output = args.output.resolve()
    state = private_dir()
    if not output.is_relative_to(state) or not output.parent.is_dir():
        parser.error("Write to an existing private subdirectory of PM_PRIVATE_DIR")
    if output.exists():
        parser.error("Use a new file")
    if not os.getenv("PM_WORKBOOK") or not os.getenv("PM_CASE_CONFIG") or not qwen.endpoint():
        parser.error("Set PM_WORKBOOK, PM_CASE_CONFIG, and a loopback PM_LLM_URL")
    report = investigation.analyze({"query": args.question})
    saved = ai_store.run(report["run_id"])
    descriptor = os.open(output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf8") as stream:
        json.dump(saved, stream, ensure_ascii=False, indent=2)
    print(json.dumps({key: saved[key] for key in ("llm_status", "termination", "seconds", "usage")}))
    return int(saved["llm_status"] != "ok")


if __name__ == "__main__":
    raise SystemExit(main())
