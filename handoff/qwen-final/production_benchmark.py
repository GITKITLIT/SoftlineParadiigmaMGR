"""Measure approved Qwen profiles against the acceptance tree. Outputs contain aggregates only."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from functools import partial
import hashlib
import json
import os
from pathlib import Path
import platform
import socket
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Barrier, Event, Thread, local
from time import perf_counter, sleep
from urllib.error import HTTPError
from urllib.request import ProxyHandler, build_opener
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from llm_lab.evaluation import scenarios
from pm import chat, investigation, qwen, service
from scripts.qwen.evaluate import exercise, streaming_probe

DIGEST = "7485fe6f11af29433bc51cab58009521f205840f5b4ae3a32fa7f92e8534fdf5"
CASES = ("simple_chat", "history", "drop", "incomparable_offers", "concurrent")
EXTRA_CASES = ("draft", "follow_up", "release_brief", "monitor", "r09")
LOCAL_ENV = {"PM_WORKBOOK": "", "PM_SNAPSHOT": "", "PM_CASE_CONFIG": "", "PM_EVIDENCE": "",
             "PM_DB_MAX_MIB": "", "PM_BACKUP_STATUS_FILE": "", "PM_MIGRATION_STATUS_FILE": "",
             "METRIKA_TOKEN": "", "METRIKA_COUNTER_ID": "", "PM_EMAIL_ENABLED": "false",
             "PM_LLM_MODEL": "qwen3-4b", "PM_LLM_SELECT_STEP": "true", "PM_QWEN_QUALITATIVE": "true",
             "PM_LLM_PROMPT_FILE": ""}
OPENER = build_opener(ProxyHandler({}), qwen.NoRedirect())
CORE_OPENER = qwen.build_opener
TELEMETRY = local()


@dataclass(frozen=True)
class Profile:
    threads: int = 4
    threads_batch: int = 8
    slots: int = 2
    context: int = 8192
    batch: int = 2048
    ubatch: int = 512
    cache_mib: int = 512
    kv: str = "f16"
    reuse: int = 0

    def command(self, binary, model, port, gpu_layers, seed):
        device = ["--device", "none"] if gpu_layers == 0 else []
        return [str(binary), "--model", str(model), "--alias", "qwen3-4b",
                "--host", "127.0.0.1", "--port", str(port), "--gpu-layers", str(gpu_layers),
                *device,
                "--threads", str(self.threads), "--threads-batch", str(self.threads_batch),
                "--ctx-size", str(self.context * self.slots), "--parallel", str(self.slots),
                "--batch-size", str(self.batch), "--ubatch-size", str(self.ubatch),
                "--cache-ram", str(self.cache_mib), "--cache-type-k", self.kv, "--cache-type-v", self.kv,
                "--cache-reuse", str(self.reuse), "--reasoning", "off",
                "--chat-template-kwargs", '{"enable_thinking":false}', "--seed", str(seed), "--metrics"]


class RecordedResponse:
    def __init__(self, response, started, row):
        self.response, self.started, self.row = response, started, row

    def __enter__(self):
        return self

    def __exit__(self, *error):
        self.row["wall_seconds"] = round(perf_counter() - self.started, 4)
        if "result" not in self.row:
            self.row["result"] = "timeout" if error[0] is TimeoutError else "incomplete_response"
        self.response.close()

    def read(self, *args):
        data = self.response.read(*args)
        try:
            obj = json.loads(data)
        except (ValueError, UnicodeError):
            self.row["result"] = "invalid_json"
            return data
        if not isinstance(obj, dict):
            self.row["result"] = "invalid_response"
            return data
        timings = obj.get("timings") or {}
        choice = next(iter(obj.get("choices") or []), {})
        self.row.update(usage=qwen.token_usage(obj), result=choice.get("finish_reason", "missing_choice"),
                        timings={key: timings[key] for key in ("cache_n", "prompt_n", "prompt_ms",
                                 "predicted_n", "predicted_ms") if isinstance(timings.get(key), (float, int))})
        return data


def instrument(*handlers, synthesis_max_tokens=None):
    opener = CORE_OPENER(*handlers)
    real_open = opener.open

    def open_recorded(request, *args, **kwargs):
        if not hasattr(TELEMETRY, "calls"):
            return real_open(request, *args, **kwargs)
        body = json.loads(request.data)
        schema = body.get("response_format", {}).get("json_schema", {})
        if schema.get("name") == "investigation" and synthesis_max_tokens is not None:
            body["max_tokens"] = min(body["max_tokens"], synthesis_max_tokens)
            request.data = json.dumps(body).encode()
        row = {"stage": schema.get("name"), "max_tokens": body["max_tokens"]}
        TELEMETRY.calls.append(row)
        started = perf_counter()
        try:
            return RecordedResponse(real_open(request, *args, **kwargs), started, row)
        except HTTPError as error:
            row.update(result=f"http_{error.code}", wall_seconds=round(perf_counter() - started, 4))
            raise
        except OSError as error:
            reason = getattr(error, "reason", error)
            row.update(result="timeout" if isinstance(reason, TimeoutError) else "transport_error",
                       wall_seconds=round(perf_counter() - started, 4))
            raise

    opener.open = open_recorded
    return opener


def counters(pid):
    if sys.platform == "linux":
        proc = Path("/proc") / str(pid)
        fields = (proc / "stat").read_text().rsplit(")", 1)[1].split()
        status = dict(line.split(":", 1) for line in (proc / "status").read_text().splitlines() if ":" in line)
        cpu = (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")
        return float(status["VmRSS"].split()[0]), float(status["VmHWM"].split()[0]), cpu
    row = subprocess.check_output(["ps", "-p", str(pid), "-o", "rss=", "-o", "time="], text=True).split()
    result = 0
    for part in row[1].split(":"):
        result = result * 60 + float(part)
    return float(row[0]), None, result


class Resources:
    def __init__(self, server, interval=0.5):
        self.server = server
        self.interval = interval
        self.rows = []
        self.stop = Event()
        self.worker = Thread(target=self.sample, daemon=True)
        self.worker.start()

    def sample(self):
        while not self.stop.is_set():
            try:
                self.rows.append((perf_counter(), counters(self.server), counters(os.getpid())))
            except (OSError, subprocess.CalledProcessError, IndexError, KeyError):
                return
            self.stop.wait(self.interval)

    def summary(self, since=0):
        rows = [row for row in self.rows if row[0] >= since]
        result = {"samples": len(rows)}
        for index, who in [(1, "llama"), (2, "harness")]:
            if rows:
                result[f"{who}_sampled_peak_rss_mib"] = round(max(row[index][0] for row in rows) / 1024, 1)
            hwm = [row[index][1] for row in rows if row[index][1] is not None]
            if hwm and index == 1:
                result["llama_process_hwm_mib"] = round(max(hwm) / 1024, 1)
            if len(rows) > 1:
                result[f"{who}_mean_cpu_pct_one_core"] = round(
                    100 * (rows[-1][index][2] - rows[0][index][2]) / (rows[-1][0] - rows[0][0]), 1)
                result[f"{who}_peak_sample_cpu_pct_one_core"] = round(max(
                    100 * (b[index][2] - a[index][2]) / (b[0] - a[0]) for a, b in zip(rows, rows[1:])), 1)
        return result


def wait_ready(process, port):
    deadline = perf_counter() + 120
    while perf_counter() < deadline and process.poll() is None:
        try:
            with OPENER.open(f"http://127.0.0.1:{port}/health", timeout=1) as response:
                if json.load(response).get("status") == "ok":
                    return
        except (OSError, ValueError):
            sleep(0.15)
    raise RuntimeError("llama-server did not become ready. Check the private startup log.")


@contextmanager
def isolated_state(*, r09=None):
    with TemporaryDirectory(prefix="qwen-production-state-") as state:
        env = LOCAL_ENV | {"PM_PRIVATE_DIR": state, "YANDEX_SEARCH_ENABLED": "false"}
        if r09:
            workbook, case, source_db = r09
            with closing(sqlite3.connect(source_db.as_uri() + "?mode=ro", uri=True)) as src, closing(
                sqlite3.connect(Path(state) / "ai.sqlite3")
            ) as dest:
                src.backup(dest)
            env.update(PM_WORKBOOK=str(workbook), PM_CASE_CONFIG=str(case))
        else:
            env.update(scenarios.WEB_ENV)
        with patch.dict(os.environ, env):
            yield


def with_calls(run):
    TELEMETRY.calls = []
    try:
        return run() | {"model_http": TELEMETRY.calls}
    finally:
        del TELEMETRY.calls


def synthetic(case, snapshot, earlier=None):
    result = exercise(case, snapshot, earlier, None)
    expected = {"simple_chat": {"backlog"}, "history": {"compare_history"}, "drop": {"sales", "funnel"},
                "incomparable_offers": {"public_competitors"}, "draft": {"sales"}, "follow_up": set(),
                "release_brief": {"releases"}, "monitor": {"sales"}}[case]
    accepted = {"ok", "not_needed"} if case in {"history", "incomparable_offers"} else {"ok"}
    result["checks"].update(live_status=result["llm_status"] in accepted,
                            model_selected_source=bool(result["model_tools"]) and expected <= set(result["model_tools"]))
    return result


def product_case(inputs):
    from openpyxl import load_workbook
    from scripts.qwen.audit_release import verify

    started = perf_counter()
    report = investigation.analyze({"query": "Почему снизились заказы после релиза R-09? "
                                    "Проверь продажи, воронку, релизы, бэклог, интервью и конкурентов."})
    elapsed = perf_counter() - started
    workbook_path, case_path, _ = inputs
    with workbook_path.open("rb") as stream:
        book = load_workbook(stream, read_only=True, data_only=True)
        try:
            case = json.loads(case_path.read_text(encoding="utf-8-sig"))
            try:
                checks, _ = verify(book, case, report)
                checks["workbook_audit_completed"] = True
            except KeyError:
                checks = {"workbook_audit_completed": False}
        finally:
            book.close()
    model_tools = [event["tool"] for event in report["trace"] if event.get("tool") and event["actor"] == "qwen"]
    hypotheses = report["hypotheses"]
    checks.update(live_status=report["llm_status"] == "ok",
                  model_selected_sales_and_funnel={"sales", "funnel"} <= set(model_tools),
                  grounded_hypothesis=bool(hypotheses) and all(
                      "orders:0" in row["evidence_ids"] for row in hypotheses),
                  cart_measurement_gap=any(row.get("relation") == "cart_measurement_gap" for row in report["findings"]))
    return {"id": "r09", "wall_seconds": round(elapsed, 3), "llm_status": report["llm_status"],
            "usage": report["usage"], "termination": report["termination"], "checks": checks,
            "hypotheses": len(hypotheses), "model_tools": model_tools}


def run_case(case, inputs):
    with isolated_state(r09=inputs if case == "r09" else None):
        if case == "r09":
            return with_calls(lambda: product_case(inputs))
        snapshot = service.overview()
        if case == "history":
            scenarios.seed_history()
        if case == "follow_up":
            setup = {}

            def prepare():
                setup["turn"] = chat.consult({"question": "Почему упали заказы Demo A? Проверь воронку."})
                report = setup["turn"]["report"]
                return {"llm_status": report["llm_status"], "usage": report["usage"]}

            prior = with_calls(prepare)
            result = with_calls(lambda: synthetic(case, snapshot, setup["turn"]))
            return result | {"prior_turn_excluded": prior}
        if case != "concurrent":
            return with_calls(lambda: synthetic(case, snapshot))
        start = perf_counter()
        together = Barrier(2)

        def client(name):
            together.wait()
            return with_calls(lambda: synthetic(name, snapshot))

        with ThreadPoolExecutor(max_workers=2) as workers:
            samples = list(workers.map(client, ("simple_chat", "draft")))
        return {"id": case, "wall_seconds": round(perf_counter() - start, 3), "samples": samples}


def hardware():
    result = {"os": platform.system(), "arch": platform.machine(), "logical_cpus": os.cpu_count()}
    result["ram_mib"] = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") // (1024 * 1024)
    if sys.platform == "linux":
        result["affinity_cpus"] = len(os.sched_getaffinity(0))
        result["model_name"] = next((line.partition(":")[2].strip() for line in
                                     Path("/proc/cpuinfo").read_text().splitlines()
                                     if line.startswith("model name")), None)
    else:
        result["model_name"] = subprocess.check_output(["sysctl", "-n", "machdep.cpu.brand_string"], text=True).strip()
    return result


def model_properties(port):
    base = f"http://127.0.0.1:{port}"
    with OPENER.open(base + "/health", timeout=4) as response:
        if json.load(response).get("status") != "ok":
            raise RuntimeError("Model is not ready")
    with OPENER.open(base + "/v1/models", timeout=4) as response:
        if not any(row.get("id") == "qwen3-4b" for row in json.load(response).get("data", [])):
            raise RuntimeError("Endpoint does not advertise the approved qwen3-4b alias")
    with OPENER.open(base + "/props", timeout=4) as response:
        properties = json.load(response)
    return properties["default_generation_settings"]["n_ctx"], properties["total_slots"]


def record_cases(args, name, sequence, resources=None):
    with patch.dict(os.environ, {"PM_LLM_URL": f"http://127.0.0.1:{args.port}/v1"}), patch.object(
        qwen, "build_opener", partial(instrument, synthesis_max_tokens=args.synthesis_max_tokens)
    ):
        for case, phase, repeat in sequence:
            start = perf_counter()
            started_at = datetime.now(timezone.utc)
            if case == "stream":
                try:
                    result = streaming_probe()
                except (OSError, ValueError) as error:
                    result = {"status": "unavailable", "error": type(error).__name__}
            else:
                result = run_case(case, args.r09)
            write(args, {"profile": name, "case": case, "phase": phase, "repeat": repeat,
                         "started_at": started_at.isoformat(), "finished_at": datetime.now(timezone.utc).isoformat(),
                         "result": result, "resources": resources.summary(start) if resources else None})


def profile_runs(args, name, settings, *, gpu_layers):
    binary = args.candidate_server if name != "baseline" and args.candidate_server else args.server
    env = {key: value for key, value in os.environ.items() if not key.startswith("LLAMA_ARG_")}
    if gpu_layers == 0 and sys.platform == "darwin":
        env["GGML_METAL_DISABLE"] = "1"
    cases = args.case or list(CASES)
    if args.r09 and not args.case:
        cases.append("r09")
    iterations = [(case, "warm", repeat + 1) for repeat in range(args.warm_repeats) for case in cases]

    def session(sequence, log_name):
        with (args.output / log_name).open("xb") as log, subprocess.Popen(
            settings.command(binary, args.model, args.port, gpu_layers, args.seed),
            stdout=log, stderr=subprocess.STDOUT, env=env
        ) as proc:
            started = perf_counter()
            resources = Resources(proc.pid)
            try:
                wait_ready(proc, args.port)
                actual_context, slots = model_properties(args.port)
                if (actual_context, slots) != (settings.context, settings.slots):
                    raise RuntimeError("llama-server applied a different context or slot count")
                write(args, {"profile": name, "event": "startup", "settings": settings.__dict__,
                             "ready_seconds": round(perf_counter() - started, 3), "server_version": args.versions[binary],
                             "reported_context_per_slot": actual_context})
                record_cases(args, name, sequence, resources)
            finally:
                resources.stop.set()
                resources.worker.join()
                write(args, {"profile": name, "event": "resources", "log": log_name, **resources.summary()})
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()

    for repeat in range(args.cold_repeats):
        cold = [(args.cold_case or cases[0], "first_after_ready", repeat + 1)]
        # Start with a fresh server / empty prompt cache. The OS page cache is left intact.
        session(cold + (iterations + [("stream", "warm", 1)] if repeat == args.cold_repeats - 1 else []),
                f"{name}-{repeat + 1}.log")
    if not args.cold_repeats:
        case, _, repeat = iterations[0]
        session([(case, "first_after_ready", repeat), *iterations[1:], ("stream", "warm", 1)],
                f"{name}-warm.log")


def write(args, record):
    with (args.output / "results.jsonl").open("a", encoding="utf-8") as stream:
        print(json.dumps(record, ensure_ascii=False), file=stream, flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    server = parser.add_mutually_exclusive_group(required=True)
    server.add_argument("--server", type=Path)
    server.add_argument("--existing-endpoint", action="store_true", help="Measure a ready model on the loopback port")
    parser.add_argument("--expected-slots", type=int, choices=(1, 2), default=1)
    parser.add_argument("--candidate-server", type=Path)
    parser.add_argument("--model", type=Path)
    parser.add_argument("--output", type=Path, required=True, help="New private output directory")
    parser.add_argument("--profile", action="append", choices=("baseline", "one-slot-baseline", "one-slot",
                                                              "two-slots", "kv-q8", "short-context", "reuse",
                                                              "no-ram-cache"))
    parser.add_argument("--case", action="append", choices=(*CASES, *EXTRA_CASES))
    parser.add_argument("--cold-case", choices=(*CASES, *EXTRA_CASES))
    parser.add_argument("--cold-repeats", type=int)
    parser.add_argument("--warm-repeats", type=int, default=3)
    parser.add_argument("--threads", type=int, default=4, help="Candidate generation threads")
    parser.add_argument("--threads-batch", type=int, default=4, help="Candidate prefill threads")
    parser.add_argument("--batch-size", type=int, default=512, help="Candidate logical batch size")
    parser.add_argument("--ubatch-size", type=int, default=256, help="Candidate physical batch size")
    parser.add_argument("--gpu-layers", type=int, default=0)
    parser.add_argument("--port", type=int)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--synthesis-max-tokens", type=int, choices=range(1, 561), metavar="1..560",
                        help="Experimental request cap in this harness only")
    parser.add_argument("--r09", type=Path, nargs=3, metavar=("WORKBOOK", "CASE", "PRIVATE_FIXTURE_DB"))
    args = parser.parse_args()
    if args.port is None:
        args.port = 18081 if args.existing_endpoint else 18181
    if args.cold_repeats is None:
        args.cold_repeats = 0 if args.existing_endpoint else 2
    if args.existing_endpoint and (args.model or args.candidate_server or args.profile or args.cold_repeats or args.cold_case):
        parser.error("Existing endpoint mode samples a ready instance without launching a server or changing its profile")
    if not args.existing_endpoint and not args.model:
        parser.error("--model is required when launching a server")
    if sys.platform not in {"darwin", "linux"}:
        parser.error("This benchmark samples process resources on macOS and Linux")
    if min(args.cold_repeats, args.warm_repeats) < 0 or not (args.cold_repeats or args.warm_repeats):
        parser.error("Give a positive number of cold and/or warm repeats")
    if min(args.threads, args.threads_batch) < 1 or not 1024 <= args.port <= 65535 or args.gpu_layers < 0:
        parser.error("Invalid thread, port or GPU count")
    if not 1 <= args.ubatch_size <= args.batch_size:
        parser.error("Require 1 <= ubatch size <= batch size")
    if ("r09" in (args.case or ()) or args.cold_case == "r09") and not args.r09:
        parser.error("R-09 requires approved workbook, case and a prepared isolated corpus database")
    if args.r09:
        args.r09 = [value.resolve(strict=True) for value in args.r09]
    os.umask(0o077)
    if args.existing_endpoint:
        try:
            context, slots = model_properties(args.port)
        except (OSError, ValueError, KeyError, RuntimeError) as error:
            parser.error(f"Loopback model probe failed: {type(error).__name__}")
        if (context, slots) != (8192, args.expected_slots):
            parser.error(f"Expected 8192 tokens and {args.expected_slots} slot(s), got {context} and {slots}")
        args.output.mkdir(mode=0o700)
        write(args, {"event": "endpoint", "reported_context_per_slot": context, "reported_slots": slots,
                     "weight_digest_verified_by_runner": False})
        cases = args.case or list(CASES)
        sequence = [(case, "existing_endpoint", repeat + 1)
                    for repeat in range(args.warm_repeats) for case in cases]
        record_cases(args, "existing-endpoint", [*sequence, ("stream", "existing_endpoint", 1)])
        return failed_checks(args.output)
    args.model = args.model.resolve(strict=True)
    with args.model.open("rb") as weights:
        if hashlib.file_digest(weights, "sha256").hexdigest() != DIGEST:
            parser.error("GGUF does not match the approved model digest")
    args.server = args.server.resolve(strict=True)
    if args.candidate_server:
        args.candidate_server = args.candidate_server.resolve(strict=True)
    args.versions = {}
    for binary in {args.server, args.candidate_server} - {None}:
        version = subprocess.run([str(binary), "--version"], check=True, capture_output=True, text=True, timeout=10)
        args.versions[binary] = (version.stdout + version.stderr).strip()
    with socket.socket() as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", args.port))
        except OSError:
            parser.error(f"Choose a free loopback port instead of {args.port}")
    args.output.mkdir(mode=0o700)
    write(args, {"event": "host", "hardware": hardware(), "gguf_sha256": DIGEST,
                 "seed": args.seed, "gpu_layers": args.gpu_layers, "synthesis_max_tokens": args.synthesis_max_tokens})
    one = Profile(threads=args.threads, threads_batch=args.threads_batch, slots=1,
                  batch=args.batch_size, ubatch=args.ubatch_size)
    profiles = {"baseline": Profile(), "one-slot-baseline": replace(Profile(), slots=1),
                "one-slot": one, "two-slots": replace(one, slots=2),
                "short-context": replace(one, context=4096), "kv-q8": replace(one, kv="q8_0"),
                "reuse": replace(one, reuse=256), "no-ram-cache": replace(one, cache_mib=0)}
    for name in args.profile or ("baseline", "one-slot"):
        profile_runs(args, name, profiles[name], gpu_layers=args.gpu_layers)
    return failed_checks(args.output)


def failed_checks(output):
    rows = [json.loads(line) for line in (output / "results.jsonl").read_text().splitlines() if line]
    assessed = [item for row in rows if row.get("case") not in {None, "stream"}
                for item in row["result"].get("samples", [row["result"]])]
    return int(any(not all(row["checks"].values()) or any(call["result"] != "stop"
                    for call in row["model_http"]) for row in assessed))


if __name__ == "__main__":
    raise SystemExit(main())
