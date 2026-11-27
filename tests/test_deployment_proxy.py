"""Synthetic TLS boundary checks. Ubuntu packages: nginx, openssl, apache2-utils."""

import base64
import hashlib
import http.client
import json
import os
import secrets
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import app
from scripts.deploy.render_proxy import render


class ProxyConfigTests(unittest.TestCase):
    def test_render_rejects_non_dns_and_unsafe_input(self):
        for address in ("localhost", "pm.test/abc", "*.pm.test", "pm.test:443", "1.2.3.4",
                        ".pm.test", "pm.\ntest", "pm.test;return"):
            with self.subTest(address=address), self.assertRaises(ValueError):
                render(address, "demo")

    def test_proxy_pins_embedded_application_script(self):
        with patch.dict(os.environ, {}, clear=True):
            html = app.page()
        script = html.split("<script>")[1].split("</script>")[0].encode()
        digest = base64.b64encode(hashlib.sha256(script).digest()).decode()
        self.assertIn(f"script-src 'sha256-{digest}'", render("pm.test", "production"))
        self.assertIn("location = /api/import", render("pm.test", "demo"))
        self.assertNotIn("location = /api/import", render("pm.test", "production"))

    def test_qwen_page_has_one_executable_script_pinned_by_proxy(self):
        with patch.dict(os.environ, {}, clear=True):
            html = app.page(qwen_chat=True)
        self.assertEqual(html.count("<script>"), 1)
        script = html.split("<script>", 1)[1].split("</script>", 1)[0]
        self.assertIn("qwen-release-report", script)
        digest = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
        config = render("pm.test", "production", qwen_chat=True)
        self.assertIn(f"script-src 'sha256-{digest}'", config)
        self.assertIn("proxy_set_header X-Paradiigma-User $remote_user;", config)
        self.assertIn("include /etc/nginx/paradiigma-qwen-proxy.inc;", config)
        self.assertNotIn("X-Paradiigma-User", render("pm.test", "production"))


NGINX = shutil.which("nginx") or ("/usr/sbin/nginx" if Path("/usr/sbin/nginx").is_file() else None)


@unittest.skipUnless(NGINX and shutil.which("htpasswd") and shutil.which("openssl"),
                     "requires nginx, htpasswd and openssl")
class ProxyHTTPTests(unittest.TestCase):
    def test_tls_auth_origin_and_backend(self):
        self.check_tls_boundary(qwen_chat=False)

    def test_tls_qwen_separates_authenticated_principals(self):
        self.check_tls_boundary(qwen_chat=True)

    def check_tls_boundary(self, *, qwen_chat):
        with ExitStack() as stack:
            work = Path(stack.enter_context(tempfile.TemporaryDirectory()))
            env = {"PM_PRIVATE_DIR": str(work / "db"), "PATH": os.environ.get("PATH", os.defpath)}
            if qwen_chat:
                from llm_lab.integration import chat_api
                proxy_key = secrets.token_urlsafe(32)
                env.update(PM_QWEN_CHAT_ENABLED="true", PM_QWEN_OWNER_MODE="proxy", PM_QWEN_PROXY_KEY=proxy_key)
                include = work / "qwen-proxy.inc"
                include.touch(mode=0o600)
                include.write_text(f'proxy_set_header X-Paradiigma-Proxy-Key "{proxy_key}";\n')
            stack.enter_context(patch.dict(os.environ, env, clear=True))
            if qwen_chat:
                chat_api.install()
            captured = []

            class Handler(app.Handler):
                def is_local(self):
                    captured.append(self.headers)
                    return super().is_local()

            backend = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            stack.callback(backend.server_close)
            thread = threading.Thread(target=backend.serve_forever, daemon=True)
            thread.start()
            stack.callback(backend.shutdown)
            with socket.socket() as port_picker:
                port_picker.bind(("127.0.0.1", 0))
                port = port_picker.getsockname()[1]

            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                            "-subj", "/CN=pm.test", "-addext", "subjectAltName=DNS:pm.test",
                            "-keyout", "key", "-out", "cert"], cwd=work, check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            password = secrets.token_urlsafe(20)
            subprocess.run(["htpasswd", "-ic", "users", "reviewer"], cwd=work, check=True,
                           input=password + "\n", text=True, capture_output=True)
            if qwen_chat:
                second_password = secrets.token_urlsafe(20)
                subprocess.run(["htpasswd", "-i", "users", "second-reviewer"], cwd=work, check=True,
                               input=second_password + "\n", text=True, capture_output=True)
            config = render("pm.test", "demo", qwen_chat=qwen_chat).replace(
                "127.0.0.1:8080", f"127.0.0.1:{backend.server_port}"
            )
            config = config.replace("listen 443 ", f"listen 127.0.0.1:{port} ")
            config = "\n".join(line for line in config.splitlines() if "listen [::]" not in line)
            for official, local in {"/etc/letsencrypt/live/pm.test/fullchain.pem": "cert",
                                    "/etc/letsencrypt/live/pm.test/privkey.pem": "key",
                                    "/etc/nginx/paradiigma.htpasswd": "users",
                                    "/etc/nginx/paradiigma-qwen-proxy.inc": "qwen-proxy.inc",
                                    "/var/log/nginx/paradiigma-access.log": "access.log",
                                    "/var/log/nginx/paradiigma-error.log": "error.log"}.items():
                config = config.replace(official, str(work / local))
            # Keep the packaged nginx's compiled-in temporary directories outside the test.
            temp_dirs = " ".join(f"{key}_temp_path {work}/{key};" for key in
                                 ("client_body", "proxy", "fastcgi", "uwsgi", "scgi"))
            (work / "nginx.conf").write_text(
                "daemon off; master_process off; error_log stderr; pid nginx.pid; "
                "events { worker_connections 128; } http { " + temp_dirs + config + " }"
            )
            command = [NGINX, "-p", str(work), "-e", "stderr", "-c", str(work / "nginx.conf")]
            subprocess.run([*command, "-t"], check=True, capture_output=True)
            process = subprocess.Popen(command, stderr=subprocess.PIPE)
            def cleanup():
                process.terminate()
                process.communicate(timeout=5)
            stack.callback(cleanup)
            for _ in range(100):
                if process.poll() is not None:
                    self.fail(process.stderr.read().decode()[-2000:])
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                        break
                except OSError:
                    time.sleep(0.05)
            else:
                self.fail("nginx did not listen")

            context = ssl.create_default_context(cafile=str(work / "cert"))
            auth = "Basic " + base64.b64encode(f"reviewer:{password}".encode()).decode()
            def request(method="GET", path="/", body=None, **headers):
                client = http.client.HTTPSConnection("pm.test", context=context, timeout=5)
                client._create_connection = lambda *a, **kw: socket.create_connection(("127.0.0.1", port))
                try:
                    client.request(method, path, body, {"Host": "pm.test", **headers})
                    response = client.getresponse()
                    return response.status, response.read()
                finally:
                    client.close()

            self.assertEqual(request()[0], 401)
            self.assertEqual(request(Authorization=auth, Host="unapproved.test")[0], 403)
            self.assertEqual(request(Authorization=auth, Origin="https://unapproved.test")[0], 403)
            self.assertEqual(request(path="/?test=hidden", Authorization=auth)[0], 400)
            self.assertEqual(len(captured), 0)
            self.assertEqual(request(Authorization=auth)[0], 200)
            headers = {"Authorization": auth, "Content-Type": "application/json"}
            api = "/api/analysis/settings"
            self.assertEqual(request("POST", api, "{}", **headers)[0], 403)
            for origin in ("null", "http://pm.test", "https://pm.test:444", "https://unapproved.test"):
                self.assertEqual(request("POST", api, "{}", Origin=origin, **headers)[0], 403)
            headers.update({"Origin": "https://pm.test", "Cookie": "fixture=private",
                            "X-Forwarded-Host": "unapproved.test", "Forwarded": "for=unknown"})
            status, body = request("POST", api, "{}", **headers)
            self.assertEqual(status, 200)
            self.assertFalse(json.loads(body)["metrika_configured"])
            self.assertEqual(len(captured), 2)
            self.assertEqual(captured[-1]["Host"], f"127.0.0.1:{backend.server_port}")
            self.assertEqual(captured[-1]["Origin"], f"http://127.0.0.1:{backend.server_port}")
            for key in ("Authorization", "Cookie", "X-Forwarded-Host", "Forwarded"):
                self.assertNotIn(key, captured[-1])
            self.assertEqual(request("POST", "/api/import", "{}", **headers)[0], 403)
            self.assertEqual(request("POST", api, " " * 150001, **headers)[0], 413)
            self.assertEqual(len(captured), 2)
            if qwen_chat:
                self.assertEqual(captured[-1]["X-Paradiigma-User"], "reviewer")
                self.assertTrue(captured[-1]["X-Paradiigma-Proxy-Key"] == proxy_key)
                second_auth = "Basic " + base64.b64encode(f"second-reviewer:{second_password}".encode()).decode()
                private_headers = {"Authorization": auth, "Origin": "https://pm.test",
                                   "Content-Type": "application/json"}

                def chat_post(action, body, *, identity=auth, expected=200):
                    status, value = request("POST", f"/api/qwen/{action}", json.dumps(body),
                                            **(private_headers | {"Authorization": identity}))
                    self.assertEqual(status, expected)
                    return json.loads(value)

                dialog = chat_post("new", {})["dialog_id"]
                question = "Что было год назад?"
                job = chat_post("start", {"dialog_id": dialog, "question": question})["job_id"]
                for _ in range(15):
                    state = chat_post("status", {"dialog_id": dialog, "job_id": job})
                    if state["status"] == "ready":
                        break
                    time.sleep(0.15)
                self.assertEqual(state["status"], "ready")
                self.assertEqual(chat_post("history", {"dialog_id": dialog})["turns"][0]["question"], question)
                self.assertEqual(chat_post("dialogs", {}, identity=second_auth)["dialogs"], [])
                chat_post("history", {"dialog_id": dialog}, identity=second_auth, expected=400)
                client = http.client.HTTPConnection("127.0.0.1", backend.server_port, timeout=5)
                try:
                    client.request("POST", "/api/qwen/dialogs", "{}", {"Content-Type": "application/json"})
                    with client.getresponse() as direct:
                        self.assertEqual(direct.status, 403)
                finally:
                    client.close()
            access_log = (work / "access.log").read_text()
            if qwen_chat:
                self.assertNotIn(proxy_key, access_log)
            for private in (password, auth, "fixture=private", "test=hidden", api):
                self.assertNotIn(private, access_log)
