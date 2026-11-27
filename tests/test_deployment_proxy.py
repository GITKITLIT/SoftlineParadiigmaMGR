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


NGINX = shutil.which("nginx") or ("/usr/sbin/nginx" if Path("/usr/sbin/nginx").is_file() else None)


@unittest.skipUnless(NGINX and shutil.which("htpasswd") and shutil.which("openssl"),
                     "requires nginx, htpasswd and openssl")
class ProxyHTTPTests(unittest.TestCase):
    def test_tls_auth_origin_and_backend(self):
        with ExitStack() as stack:
            work = Path(stack.enter_context(tempfile.TemporaryDirectory()))
            stack.enter_context(patch.dict(os.environ, {"PM_PRIVATE_DIR": str(work / "db")}, clear=True))
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
            config = render("pm.test", "demo").replace("127.0.0.1:8080", f"127.0.0.1:{backend.server_port}")
            config = config.replace("listen 443 ", f"listen 127.0.0.1:{port} ")
            config = "\n".join(line for line in config.splitlines() if "listen [::]" not in line)
            for official, local in {"/etc/letsencrypt/live/pm.test/fullchain.pem": "cert",
                                    "/etc/letsencrypt/live/pm.test/privkey.pem": "key",
                                    "/etc/nginx/paradiigma.htpasswd": "users",
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
            access_log = (work / "access.log").read_text()
            for private in (password, auth, "fixture=private", "test=hidden", api):
                self.assertNotIn(private, access_log)
