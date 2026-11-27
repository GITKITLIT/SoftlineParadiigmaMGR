"""Observe Qwen tunnel recovery on the Backend host without controlling services."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from scripts.deploy.health import probe


def tunnel_state(unit):
    try:
        output = subprocess.check_output(
            ["systemctl", "show", "--property=ActiveState,MainPID,NRestarts", "--", unit],
            text=True, timeout=4, stderr=subprocess.DEVNULL,
        )
        fields = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
        return {"active": fields["ActiveState"] == "active", "main_pid": int(fields["MainPID"]),
                "restarts": int(fields["NRestarts"])}
    except (OSError, subprocess.SubprocessError, KeyError, ValueError):
        return None


def emit(event, **data):
    print(json.dumps({"event": event, "utc": datetime.now(timezone.utc).isoformat(), **data}), flush=True)


def observe(args):
    started = time.monotonic()

    def sample():
        backend = probe(args.backend_port)
        qwen = probe(args.qwen_port, model=True)
        tunnel = tunnel_state(args.unit)
        elapsed = round(time.monotonic() - started, 3)
        emit("sample", elapsed_seconds=elapsed, backend=backend, qwen=qwen, tunnel=tunnel)
        return backend["ok"], qwen["ok"], tunnel, elapsed

    backend_ok, qwen_ok, baseline, _ = sample()
    if not (backend_ok and qwen_ok and baseline and baseline["active"] and baseline["main_pid"] > 0):
        emit("result", ok=False, reason="healthy_baseline_required")
        return 1

    emit("baseline_ready", mode=args.mode)
    first_failed_probe = None
    backend_healthy = True
    while time.monotonic() - started < args.seconds:
        time.sleep(1)
        backend_ok, qwen_ok, tunnel, elapsed = sample()
        backend_healthy &= backend_ok
        if not qwen_ok and first_failed_probe is None:
            first_failed_probe = elapsed
        if not (qwen_ok and tunnel and tunnel["active"] and tunnel["main_pid"] > 0
                and tunnel["main_pid"] != baseline["main_pid"]):
            continue
        restart_delta = tunnel["restarts"] - baseline["restarts"]
        if args.mode == "automatic" and restart_delta < 1:
            continue
        if args.mode == "stop-start" and first_failed_probe is None:
            continue
        elapsed_since_failed_probe = None if first_failed_probe is None else round(elapsed - first_failed_probe, 3)
        emit("result", ok=backend_healthy, backend_healthy_in_all_samples=backend_healthy,
             seconds_from_first_failed_probe=elapsed_since_failed_probe, tunnel_restart_delta=restart_delta,
             previous_pid=baseline["main_pid"], recovered_pid=tunnel["main_pid"])
        return 0 if backend_healthy else 1

    emit("result", ok=False, reason="replacement_and_recovery_not_observed",
         saw_qwen_unavailable=first_failed_probe is not None, backend_healthy_in_all_samples=backend_healthy)
    return 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--unit", default="paradiigma-qwen-tunnel.service")
    parser.add_argument("--backend-port", type=int, default=8080)
    parser.add_argument("--qwen-port", type=int, default=18081)
    parser.add_argument("--seconds", type=int, default=180)
    parser.add_argument("--mode", choices=("stop-start", "automatic"), default="stop-start")
    args = parser.parse_args()
    if args.seconds <= 0 or not all(1 <= port <= 65535 for port in (args.backend_port, args.qwen_port)):
        parser.error("Use a positive observation window and valid loopback ports")
    return observe(args)


if __name__ == "__main__":
    raise SystemExit(main())
