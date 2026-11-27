"""Sample Qwen's systemd cgroup and Linux host pressure as UTC-stamped JSONL."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
from time import sleep

SYSTEMD_UNIT = "paradiigma-qwen.service"
CPU_FIELDS = {"usage_usec", "nr_periods", "nr_throttled", "throttled_usec"}
EVENT_FIELDS = {"high", "oom", "oom_kill"}
HOST_MEMORY_FIELDS = {"MemAvailable", "SwapTotal", "SwapFree"}
HOST_SWAP_FIELDS = {"pswpin", "pswpout"}
HOST_CPU_FIELDS = ("user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal")


def counters(path, fields):
    try:
        lines = path.read_text().splitlines()
    except FileNotFoundError:
        return {}
    values = {}
    for line in lines:
        key, raw, *_ = line.split()
        if key.rstrip(":") in fields:
            values[key.rstrip(":")] = int(raw)
    return values


def integer(path):
    try:
        return int(path.read_text().strip())
    except FileNotFoundError:
        return None


def cpu_ticks(path):
    with path.open() as stream:
        _, *fields = stream.readline().split()
    return dict(zip(HOST_CPU_FIELDS, map(int, fields[:len(HOST_CPU_FIELDS)])))


def sample(group, proc):
    return {
        "measured_utc": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "service_cgroup_present": group.is_dir(),
        "memory_current_bytes": integer(group / "memory.current"),
        "memory_lifetime_peak_bytes": integer(group / "memory.peak"),
        "swap_current_bytes": integer(group / "memory.swap.current"),
        "memory_events": counters(group / "memory.events", EVENT_FIELDS),
        "cpu": counters(group / "cpu.stat", CPU_FIELDS),
        "host_cpu_ticks": cpu_ticks(proc / "stat"),
        "host_memory_kib": counters(proc / "meminfo", HOST_MEMORY_FIELDS),
        "host_swap_pages": counters(proc / "vmstat", HOST_SWAP_FIELDS),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interval", type=float, default=1.0, help="Sample interval in seconds")
    args = parser.parse_args()
    if not 0.2 <= args.interval <= 10:
        parser.error("Choose an interval between 0.2 and 10 seconds")
    root = Path("/sys/fs/cgroup")
    if not (root / "cgroup.controllers").is_file():
        parser.error("Linux cgroup v2 is required")
    group = subprocess.check_output(
        ["systemctl", "show", SYSTEMD_UNIT, "-p", "ControlGroup", "--value"], text=True, timeout=10
    ).strip()
    path = root / group.lstrip("/")
    if not group.startswith("/") or path == root or not path.is_dir():
        parser.error(f"Start {SYSTEMD_UNIT} before sampling")
    try:
        while True:
            print(json.dumps(sample(path, Path("/proc"))), flush=True)
            sleep(args.interval)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
