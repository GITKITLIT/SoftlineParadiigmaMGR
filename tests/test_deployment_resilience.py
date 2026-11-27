"""Exercise actual localhost requests against disposable synthetic application state."""
import http.client
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class BackendResilience(unittest.TestCase):
    def setUp(self):
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.db_dir = self.folder / "state"
        self.proc = None
        self.addCleanup(self.stop)
        self.env = {k: v for k, v in os.environ.items() if k.upper() in
                    {"SYSTEMROOT", "TEMP", "TMP", "TMPDIR", "PATH", "LANG", "WINDIR"}}
        self.env.update(PM_PRIVATE_DIR=str(self.db_dir), PYTHONUTF8="1")
        with closing(socket.socket()) as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]

    def post(self, route, body):
        with closing(http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)) as client:
            client.request("POST", route, json.dumps(body), {"Content-Type": "application/json"})
            response = client.getresponse()
            return response.status, json.loads(response.read())

    def start(self):
        self.proc = subprocess.Popen([sys.executable, "app.py", "serve", "--port", str(self.port)],
                                     cwd=ROOT, env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and self.proc.poll() is None:
            try:
                if self.post("/api/analysis/settings", {})[0] == 200:
                    return
            except OSError:
                pass
            time.sleep(0.05)
        self.fail("backend failed readiness")

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(10)

    def test_restart_during_model_request_and_fallback_after_model_stops(self):
        entered, release = threading.Event(), threading.Event()

        class WaitingModel(BaseHTTPRequestHandler):
            def do_POST(self):
                entered.set()
                release.wait(15)

            def log_message(self, *args):
                pass

        model = ThreadingHTTPServer(("127.0.0.1", 0), WaitingModel)
        threading.Thread(target=model.serve_forever, daemon=True).start()
        self.addCleanup(model.server_close)
        self.addCleanup(model.shutdown)
        self.addCleanup(release.set)
        self.start()
        with ThreadPoolExecutor(max_workers=5) as pool:
            results = list(pool.map(lambda i: self.post("/api/analyze", {"query": f"Снижение заказов {i}"}), range(5)))
            self.assertTrue(all(status == 200 for status, _ in results))
            ids = {report["run_id"] for _, report in results}
            self.assertEqual(len(ids), 5)
            self.stop()
            self.env["PM_LLM_URL"] = f"http://127.0.0.1:{model.server_port}/v1"
            self.start()
            pending = pool.submit(self.post, "/api/analyze", {"query": "Снижение заказов"})
            self.assertTrue(entered.wait(10))
            self.stop()
            with self.assertRaises((OSError, http.client.HTTPException)):
                pending.result(5)
            release.set()
            model.shutdown()
            model.server_close()
            self.start()
            status, journal = self.post("/api/analysis/journal", {})
            self.assertEqual(status, 200)
            self.assertEqual({row["run_id"] for row in journal["runs"]}, ids)
            status, result = self.post("/api/analyze", {"query": "Снижение заказов"})
            self.assertEqual(status, 200)
            self.assertEqual(result["llm_status"], "unavailable")
            self.assertTrue(result["findings"])

    def test_integration_credentials_stay_out_of_http_and_exports(self):
        canary = "synthetic-credential-" + os.urandom(12).hex()
        self.env.update(METRIKA_TOKEN=canary, METRIKA_COUNTER_ID="123",
                        YANDEX_SEARCH_API_KEY=canary, YANDEX_SEARCH_ENABLED="false")
        self.start()
        status, run = self.post("/api/analyze", {"query": "Снижение заказов"})
        self.assertEqual(status, 200)
        with closing(http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)) as client:
            client.request("GET", "/")
            response = client.getresponse()
            self.assertEqual(response.status, 200)
            self.assertNotIn(canary.encode(), response.read())
        for route, body in (("/api/analysis/settings", {}), ("/api/analysis/journal", {}),
                            ("/api/analysis/report", {"run_id": run["run_id"]}),
                            ("/api/analysis/export", {"run_id": run["run_id"]})):
            with self.subTest(route=route):
                status, data = self.post(route, body)
                self.assertEqual(status, 200)
                self.assertNotIn(canary, json.dumps(data))
        self.assertNotIn(canary, json.dumps(run))

    def test_storage_path_becomes_unavailable(self):
        self.start()
        self.db_dir.mkdir(exist_ok=True)
        self.db_dir.rename(self.folder / "offline")
        self.db_dir.write_text("temporarily unavailable")
        status, error = self.post("/api/tasks", {})
        self.assertIn(status, (400, 503))
        self.assertIn("error", error)
        self.assertNotIn(str(self.folder), json.dumps(error))
        self.db_dir.unlink()
        (self.folder / "offline").rename(self.db_dir)
        self.assertEqual(self.post("/api/tasks", {}), (200, {"tasks": []}))
