"""Import a bounded batch of saved Search captures into the integrated AI Core."""
import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pm.yandex_search.pim import history_batch, write_pim_snapshot


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--history-dir", type=Path, required=True)
    cli.add_argument("--mode", required=True, choices=("local", "synthetic"))
    cli.add_argument("--after", type=int, default=0, help="Last imported cursor, or 0 for the initial batch")
    cli.add_argument("--limit", type=int, default=20)
    args = cli.parse_args()
    state_path = os.getenv("PM_PRIVATE_DIR")
    if not state_path:
        cli.error("Set PM_PRIVATE_DIR explicitly to the approved AI Core directory")
    state = Path(state_path).expanduser().resolve()
    if ROOT in (state, *state.parents) or any((path / ".git").exists() for path in (state, *state.parents)):
        cli.error("PM_PRIVATE_DIR must be outside a repository")
    try:
        batch = history_batch(args.history_dir, after=args.after, limit=args.limit)
        cursor = args.after
        for capture in batch:
            snapshot = capture["snapshot"]
            result = write_pim_snapshot(snapshot, capture["previous"], mode=args.mode)
            cursor = capture["cursor"]
            print(json.dumps({"snapshot": snapshot["id"], "cursor": cursor,
                              "mode": args.mode, "created": result["created"]}), flush=True)
        print(json.dumps({"status": "ok", "next_cursor": cursor, "processed": len(batch)}))
    except (ImportError, OSError, ValueError, KeyError, TypeError, sqlite3.Error) as error:
        cli.exit(2, f"PIM import unavailable ({type(error).__name__}). Check the integrated checkout and paths.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
