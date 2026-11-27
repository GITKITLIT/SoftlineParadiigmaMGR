"""Use AI Core's loopback TLS server to test the deployed EnvironmentFile and CLI."""
import json
import os
import ssl
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from socketserver import TCPServer


def main(release, core_tests):
    # The parent shell requires the marker from the preceding clean-install VM test.
    if os.geteuid() != 0 or not Path("/run/paradiigma-disposable-acceptance").is_file():
        raise SystemExit("Run only on the disposable acceptance VM")
    sys.path[:0] = [str(core_tests), str(release)]
    from test_email_notifications import SmtpHandler

    env_file = Path("/etc/paradiigma/app.env")
    original = env_file.read_bytes()
    ca = Path("/var/lib/paradiigma/documents/deployment-smtp-fixture.pem")
    if ca.exists():
        raise SystemExit("Expected a fresh SMTP acceptance fixture")
    with tempfile.TemporaryDirectory() as temp, TCPServer(("127.0.0.1", 0), SmtpHandler) as server:
        key = Path(temp) / "localhost.key"
        try:
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                            "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost",
                            "-out", str(ca), "-keyout", str(key)], capture_output=True, check=True)
            ca.chmod(0o644)
            server.implicit = True
            server.reject_auth = server.disconnect_after_data = False
            server.messages, server.recipients, server.authenticated_over_tls = [], [], []
            server.data_reply = b"250 queued\r\n"
            server.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            server.context.load_cert_chain(ca, key)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                private_settings = f"""
PARADIIGMA_MODE=private
PM_EMAIL_ENABLED=true
PM_EMAIL_TEST_MODE=true
PM_SMTP_HOST=localhost
PM_SMTP_PORT={server.server_address[1]}
PM_SMTP_SECURITY=tls
PM_EMAIL_FROM=fixture@example.invalid
PM_EMAIL_TO=recipient@example.invalid
PM_EMAIL_REPORT_BASE_URL=http://127.0.0.1:8080/
SSL_CERT_FILE={ca}
"""
                env_file.write_bytes(original + private_settings.encode("ascii"))
                subprocess.run(["systemctl", "restart", "paradiigma.service"], check=True)
                cmd = ["systemd-run", "--wait", "--pipe", "--collect", "-p", "User=pm-app",
                       "-p", "Group=pm-app", "-p", "SupplementaryGroups=pm-db",
                       "-p", f"EnvironmentFile={env_file}", "-p", "UMask=0027",
                       str(release / ".venv/bin/python"), str(release / "app.py"), "email-test"]
                for _ in range(2):
                    result = subprocess.run(cmd, stdout=subprocess.PIPE, text=True, check=True)
                    assert json.loads(result.stdout)["status"] == "submitted"
                assert server.recipients == ["<recipient@example.invalid>"]
                assert len(server.messages) == 1
            finally:
                server.shutdown()
                thread.join(timeout=5)
        finally:
            env_file.write_bytes(original)
            ca.unlink(missing_ok=True)
            subprocess.run(["systemctl", "restart", "paradiigma.service"], check=True)
    print("Deployed SMTP configuration, loopback TLS delivery and idempotence verified")


if __name__ == "__main__":
    main(*(Path(arg).resolve() for arg in sys.argv[1:]))
