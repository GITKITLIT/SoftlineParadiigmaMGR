"""Run the isolated synthetic acceptance using an existing Qwen SSH forward."""
import json
import os
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer
from pathlib import Path
import sys
from threading import Thread
from time import monotonic, sleep
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, ProxyHandler, Request, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import app
from llm_lab.evaluation import scenarios
from llm_lab.integration import chat_api, chat_store
from pm import live_runs, qwen
import production_benchmark as bench


def main():
    os.environ.update(PM_LLM_URL="http://127.0.0.1:18081/v1", PM_QWEN_OWNER_MODE="local",
                      PM_QWEN_CHAT_ENABLED="true", PM_QWEN_RUNTIME_PROFILE="sandy-bridge-one-slot",
                      PM_QWEN_STABLE_PLANNER_PREFIX="true")
    if bench.model_properties(18081) != (8192, 1):
        raise SystemExit("Unexpected model profile")
    probe = build_opener(ProxyHandler({}))
    with probe.open("http://127.0.0.1:18081/slots", timeout=4) as response:
        if any(slot.get("is_processing", True) for slot in json.load(response)):
            raise SystemExit("Model slot is busy")

    class Handler(app.Handler):
        def log_message(self, *_):
            pass

    start = live_runs.start
    calls = []

    def tracked(request, analyze=app.investigation.analyze, **kwargs):
        def recorded(body, **options):
            bench.TELEMETRY.calls = []
            try:
                return analyze(body, **options)
            finally:
                calls.extend(bench.TELEMETRY.calls)
                del bench.TELEMETRY.calls
        return start(request, recorded, **kwargs)

    with bench.isolated_state(), patch.object(live_runs, "start", tracked), patch.object(qwen, "build_opener", bench.instrument):
        chat_api.install()
        scenarios.seed_history()
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()

        def client():
            opener = build_opener(ProxyHandler({}), HTTPCookieProcessor(CookieJar()))

            def post(route, body, *, status=200):
                req = Request(f"http://127.0.0.1:{server.server_port}/api/{route}",
                              json.dumps(body).encode(), {"Content-Type": "application/json"})
                try:
                    with opener.open(req, timeout=10) as reply:
                        result = reply.status, json.load(reply)
                except HTTPError as error:
                    result = error.code, None
                if result[0] != status:
                    raise RuntimeError(f"{route} returned {result[0]}")
                return result[1]
            return post

        post = client()

        def wait(route, body, name):
            calls.clear()
            started = monotonic()
            job = post(route + "/start", body)
            while monotonic() - started < job["wait_seconds"]:
                state = post(route + "/status", {**body, "job_id": job["job_id"]})
                if state["status"] == "ready":
                    return state, round(monotonic() - started, 3)
                if state["status"] == "failed":
                    raise RuntimeError(f"{name} worker failed")
                sleep(0.7)
            raise TimeoutError(name)

        def record(case, seconds, report, checks):
            print(json.dumps({"scenario": case, "wall_seconds": seconds, "llm_status": report["llm_status"],
                              "checks": checks, "model_http": calls}, ensure_ascii=True), flush=True)
            assert all(checks.values()) and calls and all(
                x["result"] == "stop" and not x.get("truncated") for x in calls)

        try:
            state, seconds = wait("analysis", {"query": scenarios.CASES["drop"]}, "drop")
            run = state["run_id"]
            report = post("analysis/report", {"run_id": run})
            checks = scenarios.checks("drop", report)
            selected = {step.get("tool") for step in report["trace"] if step.get("actor") == "qwen"}
            checks.update(live=report["llm_status"] == "ok", selected={"sales", "funnel"} <= selected,
                          hypothesis=bool(report["hypotheses"]), draft=bool(report["proposed_tasks"]))
            record("drop", seconds, report, checks)
            assert not post("tasks", {})["tasks"]
            proposal = report["proposed_tasks"][0]
            decision = {"run_id": run, "proposal_id": proposal["id"], "action": "accept",
                        "idempotency_key": "isolated-tunnel-acceptance", "target": 110}
            task = post("tasks/decide", decision)
            assert post("tasks/decide", decision)["id"] == task["id"]
            assert post("tasks/review", {"task_id": task["id"]})["status"] == "waiting_for_data"
            dialog = post("qwen/new", {})["dialog_id"]
            body = {"dialog_id": dialog, "question": scenarios.CASES["history"],
                    "scope": {"memory": scenarios.scope_for("Demo A")}}
            state, seconds = wait("qwen", body, "history")
            with chat_store.database() as db:
                turn = json.loads(db.execute("SELECT body FROM qwen_turns WHERE dialog=?",
                                            (dialog,)).fetchone()[0])
            historical = turn["report"]
            selected = {step.get("tool") for step in historical["trace"] if step.get("actor") == "qwen"}
            record("history", seconds, historical, scenarios.checks("history", historical) |
                   {"live_selection": "compare_history" in selected})
            os.environ["PM_LLM_URL"] = ""
            assert post("analysis/report", {"run_id": run}) == report
            assert len(post("qwen/history", {"dialog_id": dialog})["turns"]) == 1
            other = client()
            other("qwen/new", {})
            other("qwen/history", {"dialog_id": dialog}, status=400)
            assert len(post("tasks", {})["tasks"]) == 1
            print(json.dumps({"approval": "ok", "saved_report_without_model": "ok",
                              "saved_history_without_model": "ok", "owner_isolation": "ok"}), flush=True)
        finally:
            server.shutdown()
            worker.join()
            server.server_close()


if __name__ == "__main__":
    main()