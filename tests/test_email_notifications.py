"""SMTP integration and durable notification boundaries on synthetic fixtures."""
import email
import json
import os
from pathlib import Path
import shutil
import smtplib
import socketserver
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import app
from pm import ai_store, email_notifications as mail, investigation, monitor
from pm.settings import ROOT


class SmtpHandler(socketserver.BaseRequestHandler):
    def handle(self):
        server = self.server
        conn = self.request
        reader = None
        encrypted = server.implicit
        try:
            if encrypted:
                conn = server.context.wrap_socket(conn, server_side=True)
            reader = conn.makefile("rb")
            conn.sendall(b"220 test server\r\n")
            while line := reader.readline():
                command, _, arg = line.strip().partition(b" ")
                command = command.upper()
                if command == b"EHLO":
                    response = b"250-localhost\r\n250 AUTH PLAIN\r\n" if encrypted else b"250-localhost\r\n250 STARTTLS\r\n"
                elif command == b"STARTTLS":
                    conn.sendall(b"220 ready\r\n")
                    reader.close()
                    conn = server.context.wrap_socket(conn, server_side=True)
                    reader = conn.makefile("rb")
                    encrypted = True
                    continue
                elif command == b"AUTH":
                    self.server.authenticated_over_tls.append(encrypted)
                    response = b"535 fixture-auth-failure do-not-log\r\n" if server.reject_auth else b"235 OK\r\n"
                elif command == b"RCPT":
                    server.recipients.append(arg.decode("ascii").removeprefix("to:"))
                    response = b"250 OK\r\n"
                elif command == b"DATA":
                    conn.sendall(b"354 continue\r\n")
                    lines = []
                    while (line := reader.readline()) not in {b".\r\n", b""}:
                        lines.append(line)
                    server.messages.append(b"".join(lines))
                    if server.disconnect_after_data:
                        return
                    response = server.data_reply
                else:
                    response = b"250 OK\r\n"
                conn.sendall(response)
        except (OSError, ssl.SSLError):
            return
        finally:
            if reader:
                reader.close()
            if conn is not self.request:
                conn.close()


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ, {
            "PM_PRIVATE_DIR": str(self.directory),
            "PM_WORKBOOK": "", "PM_SNAPSHOT": "", "PM_CASE_CONFIG": "", "PM_EVIDENCE": "",
            "PM_LLM_URL": "", "PM_MIGRATION_STATUS_FILE": "", "YANDEX_SEARCH_ENABLED": "false",
            "PM_MONITOR_DAILY_LIMIT": "4", "PM_EMAIL_ENABLED": "false", "PM_EMAIL_TEST_MODE": "true",
            "PM_SMTP_HOST": "localhost", "PM_SMTP_PORT": "", "PM_SMTP_SECURITY": "starttls",
            "PM_SMTP_USERNAME": "fixture-user", "PM_SMTP_PASSWORD": "fixture-password",
            "PM_EMAIL_FROM": "test-sender@example.invalid", "PM_EMAIL_TO": "approved@example.invalid",
            "PM_EMAIL_REPORT_BASE_URL": "https://protected.example.invalid", "PM_EMAIL_DAILY_LIMIT": "4",
        }))

    def local_server(self, *, implicit=False, reject_auth=False, disconnect=False, trusted=True,
                     data_reply=b"250 queued\r\n"):
        openssl = shutil.which("openssl")
        if openssl is None:
            self.skipTest("OpenSSL CLI required for disposable TLS certificates")
        cert, key = self.directory / "cert.pem", self.directory / "cert-key.pem"
        if not cert.exists():
            subprocess.run(
                [openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                 "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost", "-out", str(cert),
                 "-keyout", str(key)],
                capture_output=True, check=True,
            )
        server = self.enterContext(socketserver.TCPServer(("127.0.0.1", 0), SmtpHandler))
        server.implicit, server.reject_auth, server.disconnect_after_data = implicit, reject_auth, disconnect
        server.messages, server.recipients, server.authenticated_over_tls = [], [], []
        server.data_reply = data_reply
        server.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server.context.load_cert_chain(cert, key)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.shutdown)
        self.enterContext(patch.dict(os.environ, {
            "PM_EMAIL_ENABLED": "true", "PM_SMTP_PORT": str(server.server_address[1]),
            "PM_SMTP_SECURITY": "tls" if implicit else "starttls",
            "SSL_CERT_FILE": str(cert) if trusted else "",
        }))
        return server

    def journal(self):
        return app.api("/api/analysis/journal", {})["notifications"][0]

    def test_disabled_monitor_preserves_tag_and_redmine_export(self):
        with patch.object(mail, "smtp_submit") as submit:
            first = monitor.check({}, proactive=True)
            second = monitor.check({}, proactive=True)
        submit.assert_not_called()
        self.assertTrue(first["notification"])
        self.assertFalse(second["notification"])
        self.assertEqual(self.journal()["email_status"], "disabled")
        self.assertEqual(mail.send_test(), {"status": "disabled"})
        report = ai_store.run(first["run_id"])
        proposal = report["proposed_tasks"][0]
        task = app.api("/api/tasks/decide", {"run_id": first["run_id"], "proposal_id": proposal["id"],
                                           "action": "accept", "target": 105, "idempotency_key": "approved"})
        export = app.api("/api/tasks/redmine-export", {"task_id": task["id"]})
        self.assertEqual(app.api("/api/tasks", {})["tasks"][0]["tag"], ai_store.TASK_TAG)
        self.assertEqual(export["tag"], ai_store.TASK_TAG)
        self.assertIn(ai_store.TASK_TAG, export["payload"]["issue"]["description"])

    def test_real_starttls_submission_uses_saved_data_and_fixed_recipients(self):
        server = self.local_server()
        captured = {}

        def analyze(request):
            report = investigation.analyze(request | {"recipient": "random@example.invalid"}, persist=False)
            report["summary"] = "Private interview details"
            report["hypotheses"][0]["verify"] = "Confidential recommended action"
            ai_store.save_run(report)
            captured["run_id"] = report["run_id"]
            return report | {"run_id": captured["run_id"], "recipient": "random@example.invalid"}

        monitor.check({}, proactive=True, analyze=analyze)
        monitor.check({}, proactive=True)
        record = self.journal()
        self.assertEqual((record["email_status"], record["email_attempts"]), ("submitted", 1))
        self.assertEqual(server.authenticated_over_tls, [True])
        self.assertEqual(server.recipients, ["<approved@example.invalid>"])
        self.assertEqual(len(server.messages), 1)
        message = email.message_from_bytes(server.messages[0], policy=email.policy.default)
        text = message.get_content()
        self.assertIn("ТЕСТ", message["Subject"])
        self.assertIn("-30.0%", text)
        self.assertIn("существенный сигнал", text)
        self.assertIn("#report=" + captured["run_id"], text)
        self.assertRegex(text, r"UTC\): \d{4}-\d\d-\d\d")
        for sensitive in ("random@example.invalid", "approved@example.invalid", "fixture-password",
                          "fixture-user", "Private interview details", "Confidential recommended action"):
            self.assertNotIn(sensitive, str(message))
            self.assertNotIn(sensitive, json.dumps(record))

    def test_cli_test_uses_implicit_tls_and_is_idempotent(self):
        server = self.local_server(implicit=True)
        for _ in range(2):
            cmd = [sys.executable, "-B", "-X", "utf8", str(ROOT / "app.py"), "email-test"]
            result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf8", check=True)
            self.assertEqual(json.loads(result.stdout), {"status": "submitted", "attempts": 1, "reason": None})
        self.assertEqual(len(server.messages), 1)
        self.assertIn("#journal", email.message_from_bytes(server.messages[0], policy=email.policy.default).get_content())

    def test_invalid_certificate_is_rejected_before_auth(self):
        server = self.local_server(trusted=False)
        monitor.check({}, proactive=True)
        record = self.journal()
        self.assertEqual((record["email_status"], record["email_reason"]), ("failed", "tls_or_smtp_configuration"))
        self.assertFalse(server.authenticated_over_tls)
        self.assertFalse(server.messages)

    def test_rejected_auth_is_terminal_and_response_is_private(self):
        server = self.local_server(reject_auth=True)
        monitor.check({}, proactive=True)
        record = self.journal()
        self.assertEqual((record["email_status"], record["email_reason"]), ("failed", "authentication"))
        self.assertNotIn("do-not-log", json.dumps(record))
        self.assertFalse(server.messages)

    def test_retry_after_restart_and_finite_attempts(self):
        server = self.local_server()
        self.enterContext(patch.dict(os.environ, {"PM_EMAIL_DAILY_LIMIT": "8"}))
        with patch.object(smtplib, "SMTP", side_effect=ConnectionRefusedError("do-not-log")):
            result = monitor.check({}, proactive=True)
        self.assertEqual(result["status"], "signal")
        self.assertEqual(self.journal()["email_status"], "retry")
        self.assertNotIn("do-not-log", json.dumps(self.journal()))
        future = datetime.now(timezone.utc) + timedelta(seconds=65)
        script = ("import sys; from datetime import datetime; from pm.email_notifications import deliver_pending;"
                  "deliver_pending(clock=lambda: datetime.fromisoformat(sys.argv[1]))")
        child_env = dict(os.environ, PYTHONPATH=str(ROOT))
        subprocess.run([sys.executable, "-B", "-X", "utf8", "-c", script, future.isoformat()],
                       env=child_env, check=True)
        self.assertEqual(self.journal()["email_attempts"], 2)
        self.assertEqual(self.journal()["email_status"], "submitted")
        self.assertEqual(len(server.messages), 1)

        with patch.object(smtplib, "SMTP", side_effect=ConnectionRefusedError):
            monitor.check({"alert_percent": 19}, proactive=True)
            mail.deliver_pending(clock=lambda: future)
            mail.deliver_pending(clock=lambda: future + timedelta(minutes=6))
            mail.deliver_pending(clock=lambda: future + timedelta(minutes=12))
        self.assertEqual((self.journal()["email_status"], self.journal()["email_attempts"]), ("failed", 3))

    def test_explicit_temporary_data_reply_is_retried(self):
        server = self.local_server(data_reply=b"451 busy\r\n")
        monitor.check({}, proactive=True)
        self.assertEqual((self.journal()["email_status"], self.journal()["email_reason"]), ("retry", "smtp_rejected"))
        server.data_reply = b"250 queued\r\n"
        mail.deliver_pending(clock=lambda: datetime.now(timezone.utc) + timedelta(minutes=2))
        self.assertEqual(self.journal()["email_status"], "submitted")
        ids = [email.message_from_bytes(raw)["Message-ID"] for raw in server.messages]
        self.assertEqual(len(ids), 2)
        self.assertEqual(ids[0], ids[1])

    def test_uncertain_data_reply_never_retries(self):
        server = self.local_server(disconnect=True)
        monitor.check({}, proactive=True)
        mail.deliver_pending(clock=lambda: datetime.now(timezone.utc) + timedelta(hours=1))
        record = self.journal()
        self.assertEqual((record["email_status"], record["email_attempts"]), ("uncertain", 1))
        self.assertEqual(len(server.messages), 1)

    def test_daily_limit_and_expired_lease_do_not_resend(self):
        with patch.dict(os.environ, {"PM_EMAIL_ENABLED": "true", "PM_EMAIL_DAILY_LIMIT": "1"}):
            with patch.object(mail, "deliver_pending"):
                monitor.check({"alert_percent": 19}, proactive=True)
                monitor.check({"alert_percent": 18}, proactive=True)
            _, settings = mail.settings()
            current = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
            self.assertIsNotNone(mail.claim(settings, current, None))
            with patch.object(mail, "smtp_submit", return_value=("submitted", None)) as submit:
                mail.deliver_pending(clock=lambda: current + timedelta(minutes=11))
                statuses = {row["email_status"] for row in ai_store.notifications()}
                self.assertEqual(statuses, {"uncertain", "pending"})
                submit.assert_not_called()
                mail.deliver_pending(clock=lambda: current + timedelta(days=2))
                self.assertIn("uncertain", {row["email_status"] for row in ai_store.notifications()})

    def test_changed_destination_blocks_pending_delivery(self):
        with patch.dict(os.environ, {"PM_EMAIL_ENABLED": "true"}):
            with patch.object(mail, "deliver_pending"):
                monitor.check({}, proactive=True)
            with patch.dict(os.environ, {"PM_EMAIL_TO": "different@example.invalid"}):
                with patch.object(mail, "smtp_submit") as submit:
                    mail.deliver_pending()
                submit.assert_not_called()
        self.assertEqual(self.journal()["email_reason"], "destination_changed")

    def test_test_mode_blocks_local_reports_and_live_content_is_minimal(self):
        with patch.dict(os.environ, {"PM_EMAIL_ENABLED": "true", "PM_SNAPSHOT": str(ROOT / "data/demo-overview.json")}):
            with patch.object(mail, "smtp_submit") as submit:
                monitor.check({}, proactive=True)
            submit.assert_not_called()
            self.assertEqual(self.journal()["email_status"], "blocked_test_mode")
            with patch.dict(os.environ, {"PM_EMAIL_TEST_MODE": "false"}):
                with patch.object(mail, "smtp_submit", return_value=("submitted", None)) as submit:
                    monitor.check({"alert_percent": 19}, proactive=True)
                text = submit.call_args.args[1].get_content()
                self.assertNotIn("-30.0%", text)
                self.assertNotIn("синтетические данные", text)
                self.assertIn("существенный сигнал", text)

    def test_insecure_or_injected_configuration_never_sends(self):
        with patch.dict(os.environ, {"PM_EMAIL_ENABLED": "true", "PM_EMAIL_TO":
                                     "approved@example.invalid\r\nBcc: arbitrary@example.invalid"}):
            with patch.object(mail, "smtp_submit") as submit:
                monitor.check({}, proactive=True)
            submit.assert_not_called()
            self.assertEqual(self.journal()["email_status"], "configuration_error")
        with patch.dict(os.environ, {"PM_EMAIL_ENABLED": "true", "PM_EMAIL_REPORT_BASE_URL": "http://example.invalid"}):
            self.assertEqual(mail.send_test(), {"status": "configuration_error"})


if __name__ == "__main__":
    unittest.main()
