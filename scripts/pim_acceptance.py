"""Isolated acceptance rehearsal. All inputs and temporary database failures are synthetic."""
import json
import os
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
from pm import ai_store, history, memory, monitor
from pm.settings import ROOT
from scripts import pim_backup


class LocalAPI:
    def __enter__(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)

    def post(self, path, body):
        request = Request(f"http://127.0.0.1:{self.server.server_port}/api/{path}",
                          data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=12) as response:
            return json.load(response)


def point(day, value):
    return dict(mode="synthetic", stream="sales", source="acceptance_fixture", product="Synthetic A",
                metric="orders", unit="orders", aggregation="sum", start=day, value=value, quality="complete")


def main():
    with tempfile.TemporaryDirectory() as folder:
        base = Path(folder)
        state = base / "state"
        null_environment = {name: "" for name in os.environ if name.startswith(("PM_", "METRIKA_", "YANDEX_SEARCH_"))}
        with patch.dict(os.environ, null_environment | {"PM_PRIVATE_DIR": str(state)}):
            memory.write_observations([point("2024-05-08", 20), point("2025-05-08", 25)])
            ai_store.store_excerpts("fixture:research", [{"id": "proof", "kind": "Интервью",
                                                          "ref": "Synthetic research", "text": "Корзина может быть непонятной"}])
            with LocalAPI() as local:
                signal = monitor.check({}, proactive=True)
                assert signal["status"] == "signal"
                report = local.post("analysis/report", {"run_id": signal["run_id"]})
                assert report["hypotheses"] and report["evidence"]
                proposal = report["proposed_tasks"][0]["id"]
                task = local.post("tasks/decide", {"run_id": report["run_id"], "proposal_id": proposal,
                                  "action": "accept", "idempotency_key": "synthetic-approval", "target": 110})
                observations = json.loads((ROOT / "data/effect-demo.json").read_text(encoding="utf8"))
                effect = local.post("tasks/review", {"task_id": task["id"], "mode": "simulation",
                                                    "observation": observations})
                assert effect["mode"] == "simulation" and effect["status"] == "achieved"
                backup, metadata = pim_backup.create(state / pim_backup.DB_NAME, base / "copies")
                pim_backup.verify(backup)
                dropped = datetime.now(timezone.utc)
                memory.write_observations([point("2026-05-08", 32)])
            lost = state / pim_backup.DB_NAME
            # Only this synthetic acceptance database is deliberately made unavailable.
            lost.rename(base / "removed.sqlite3")
            lost.write_bytes(b"synthetic corruption of the working copy")
            start = time.monotonic()
            restored = base / "restored"
            pim_backup.restore(backup, restored)
            with patch.dict(os.environ, {"PM_PRIVATE_DIR": str(restored)}), LocalAPI() as local:
                assert local.post("tasks", {})["tasks"][0]["id"] == task["id"]
                assert local.post("analysis/report", {"run_id": report["run_id"]})["hypotheses"]
                assert local.post("memory/search", {"mode": "local", "query": "корзина"})["documents"]
                series = next(row["id"] for row in local.post("memory/series", {"mode": "synthetic"})["series"]
                              if row["product"] == "Synthetic A")
                compare = local.post("memory/compare", {"mode": "synthetic", "series_id": series,
                                     "before": ["2024-05-08", "2024-05-08"], "after": ["2025-05-08", "2025-05-08"]})
                assert compare["delta"] == 5
                elapsed = time.monotonic() - start
                assert memory.write_observations([point("2026-05-08", 32)])["created"] == 1
            print(json.dumps({
                "status": "ok", "mode": "synthetic", "working_database_unavailable": True,
                "acceptance_steps": ["signal", "analysis", "evidence", "hypothesis", "approval",
                                     "effect_simulation", "backup", "restore", "historical_compare", "new_write"],
                "rto_to_http_verification_seconds": round(elapsed, 3),
                "snapshot_at": metadata["created"],
                "rpo_rehearsal_lost_updates": 1,
                "snapshot_to_unprotected_write_seconds": round(
                    (dropped - datetime.fromisoformat(metadata["created"])).total_seconds(), 3),
            }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
