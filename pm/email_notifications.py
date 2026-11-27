"""Local, durable email delivery for completed monitoring investigations."""
import hashlib
import json
import math
import os
import re
import smtplib
import ssl
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import format_datetime
from urllib.parse import urlsplit

from . import ai_store

ADDRESS = re.compile(r"[A-Za-z0-9_+%-]+(?:\.[A-Za-z0-9_+%-]+)*@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
HOST = re.compile(r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*")
RUN_ID = re.compile(r"[0-9a-f]{32}")
RETRY_DELAYS = (60, 300)
LEASE = timedelta(minutes=10)
MAX_AGE = timedelta(days=1)


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    security: str
    sender: str
    recipients: tuple[str, ...]
    report_url: str
    daily_limit: int
    test_mode: bool
    username: str = field(repr=False)
    password: str = field(repr=False)

    @property
    def destination(self):
        values = (self.host, self.port, self.security, self.sender, self.recipients,
                  self.report_url, self.test_mode)
        return hashlib.sha256(ai_store.dumps(values).encode()).hexdigest()


def flag(name, default):
    value = os.getenv(name, default).strip().lower()
    if value not in {"true", "false"}:
        raise ValueError
    return value == "true"


def settings():
    """Return a journal status and SMTP settings, without exposing invalid values."""
    try:
        if not flag("PM_EMAIL_ENABLED", "false"):
            return "disabled", None
        security = os.getenv("PM_SMTP_SECURITY", "starttls")
        host = os.getenv("PM_SMTP_HOST", "")
        port = int(os.getenv("PM_SMTP_PORT") or ("465" if security == "tls" else "587"))
        sender = os.getenv("PM_EMAIL_FROM", "")
        recipients = tuple(sorted(set(item.strip() for item in os.getenv("PM_EMAIL_TO", "").split(","))))
        username = os.getenv("PM_SMTP_USERNAME", "")
        password = os.getenv("PM_SMTP_PASSWORD", "")
        limit = int(os.getenv("PM_EMAIL_DAILY_LIMIT", "4"))
        url = os.getenv("PM_EMAIL_REPORT_BASE_URL", "")
        parsed = urlsplit(url)
        if (security not in {"tls", "starttls"} or not HOST.fullmatch(host) or len(host) > 253
                or not 1 <= port <= 65535 or not 1 <= limit <= 20 or not 1 <= len(recipients) <= 5
                or not all(len(address) <= 254 and ADDRESS.fullmatch(address) for address in (sender, *recipients))
                or bool(username) != bool(password) or not username.isascii() or not password.isascii()
                or not url.isascii() or any(ord(ch) <= 32 for ch in url)
                or parsed.path not in {"", "/"} or any(ch in url for ch in "@?#\\")
                or not HOST.fullmatch(parsed.hostname or "")
                or (parsed.port is not None and not 1 <= parsed.port <= 65535)
                or (parsed.scheme != "https" and
                    (parsed.scheme, parsed.hostname) not in {("http", "localhost"), ("http", "127.0.0.1")})):
            raise ValueError
        return "pending", Settings(host, port, security, sender, recipients, url.rstrip("/") + "/",
                                   limit, flag("PM_EMAIL_TEST_MODE", "true"), username, password)
    except ValueError:
        return "configuration_error", None


def enqueue(db, delivery_id, created, *, investigation_id=None):
    status, config = settings()
    db.execute("""INSERT OR IGNORE INTO email_outbox(id, investigation_id, created, status, destination)
                  VALUES (?, ?, ?, ?, ?)""",
               (delivery_id, investigation_id, created, status, config.destination if config else None))


def claim(config, current, selected_id):
    stamp = current.isoformat()
    today = current.date().isoformat()
    with ai_store.database() as db:
        db.execute("""UPDATE email_outbox SET status='uncertain', reason='lease_expired', next_at=NULL
                      WHERE status='sending' AND next_at <= ?""", (stamp,))
        db.execute("""UPDATE email_outbox SET status='expired', next_at=NULL
                      WHERE status IN ('pending', 'retry') AND created <= ?""", ((current - MAX_AGE).isoformat(),))
        db.execute("""UPDATE email_outbox SET status='configuration_error', reason='destination_changed', next_at=NULL
                      WHERE status IN ('pending', 'retry') AND destination != ?""", (config.destination,))
        if db.execute("SELECT 1 FROM email_outbox WHERE status='sending' LIMIT 1").fetchone():
            return None
        quota = db.execute("SELECT attempts FROM email_daily_usage WHERE utc_day=?", (today,)).fetchone()
        if quota and quota["attempts"] >= config.daily_limit:
            return None
        query = """SELECT e.*, q.run_id, q.created AS detected, r.report FROM email_outbox e
                   LEFT JOIN investigation_queue q ON q.id=e.investigation_id
                   LEFT JOIN runs r ON r.id=q.run_id
                   WHERE e.status IN ('pending', 'retry') AND (e.next_at IS NULL OR e.next_at <= ?)
                     AND (? IS NULL OR e.id=?) ORDER BY e.created, e.id LIMIT 1"""
        row = db.execute(query, (stamp, selected_id, selected_id)).fetchone()
        if row is None:
            return None
        body = message(config, row)
        if body is None:
            db.execute("""UPDATE email_outbox SET status='blocked_test_mode', next_at=NULL
                          WHERE id=?""", (row["id"],))
            return None
        db.execute("""UPDATE email_outbox SET status='sending', attempts=attempts+1, next_at=?, reason=NULL
                      WHERE id=?""", ((current + LEASE).isoformat(), row["id"]))
        db.execute("""INSERT INTO email_daily_usage VALUES (?, 1)
                      ON CONFLICT(utc_day) DO UPDATE SET attempts=attempts+1""", (today,))
        return row["id"], row["attempts"] + 1, body


def message(config, row):
    cli_test = row["investigation_id"] is None
    run_id = row["run_id"]
    report = {} if cli_test else json.loads(row["report"] or "{}")
    synthetic = cli_test or report.get("data_mode") == "synthetic"
    if not cli_test and (not RUN_ID.fullmatch(run_id or "") or not report or (config.test_mode and not synthetic)):
        return None
    metric = None
    if synthetic and not cli_test:
        primary = next((item for item in report.get("evidence", [])
                        if (item.get("id"), item.get("kind"), item.get("data_mode"), item.get("metric"))
                        == ("orders:0", "orders", "synthetic", "orders_index")), {})
        value = primary.get("values", {}).get("delta")
        if type(value) in {int, float} and -100 <= value < 0 and math.isfinite(value):
            metric = f"Изменение индекса заказов: {value:+.1f}%."
    subject = "ParadiigmaMGR · проверка доставки" if cli_test else "ParadiigmaMGR · сигнал мониторинга"
    problem = ("Проверка доставки на синтетическом примере." if cli_test
               else "Индекс заказов снизился до порога мониторинга. Расследование завершено.")
    step = ("Подтвердить получение письма в тестовом ящике." if cli_test
            else "Открыть отчёт, проверить гипотезы и сравнить основной и контрольный ряды.")
    date = (row["detected"] or row["created"])[:10]
    link = config.report_url + ("#journal" if cli_test else "#report=" + run_id)
    lines = [
        "ТЕСТ · синтетические данные" if synthetic else None,
        "Продукт: ParadiigmaMGR.", problem, metric,
        "Значимость: тест." if cli_test else "Значимость: существенный сигнал.",
        f"Дата обнаружения (UTC): {date}.", f"Следующий шаг: {step}",
        f"Защищённый {'журнал' if cli_test else 'отчёт'}: {link}",
    ]
    email = EmailMessage(policy=SMTP)
    email["Subject"] = ("[ТЕСТ] " if synthetic else "") + subject
    email["From"] = config.sender
    email["Date"] = format_datetime(datetime.fromisoformat(row["created"]))
    email["To"] = "undisclosed-recipients:;"
    email["Message-ID"] = f"<{row['id']}@{config.sender.split('@')[1]}>"
    email.set_content("\n".join(line for line in lines if line is not None))
    return email


def smtp_submit(config, body):
    """Return an outcome and a fixed diagnostic code, including DATA uncertainty."""
    client = None
    in_data = False
    try:
        tls = ssl.create_default_context()
        if config.security == "tls":
            client = smtplib.SMTP_SSL(config.host, config.port, timeout=8, context=tls)
        else:
            client = smtplib.SMTP(config.host, config.port, timeout=8)
            code, response = client.ehlo()
            if code != 250:
                raise smtplib.SMTPHeloError(code, response)
            client.starttls(context=tls)
        code, response = client.ehlo()
        if code != 250:
            raise smtplib.SMTPHeloError(code, response)
        if config.username:
            client.login(config.username, config.password)
        for command, addresses in ((client.mail, (config.sender,)), (client.rcpt, config.recipients)):
            for address in addresses:
                code, response = command(address)
                if not 200 <= code < 300:
                    raise smtplib.SMTPResponseException(code, response)
        raw = body.as_bytes()
        in_data = True
        code, response = client.data(raw)
        if code != 250:
            raise smtplib.SMTPDataError(code, response)
        return "submitted", None
    except smtplib.SMTPResponseException as error:
        reason = "authentication" if isinstance(error, smtplib.SMTPAuthenticationError) else "smtp_rejected"
        if 400 <= error.smtp_code < 500:
            return "retry", reason
        if 500 <= error.smtp_code < 600 or not in_data:
            return "failed", reason
        return "uncertain", "smtp_outcome_unknown"
    except (ssl.SSLError, smtplib.SMTPNotSupportedError):
        return ("uncertain", "smtp_outcome_unknown") if in_data else ("failed", "tls_or_smtp_configuration")
    except OSError:
        return ("uncertain", "smtp_outcome_unknown") if in_data else ("retry", "smtp_unavailable")
    finally:
        if client:
            try:
                client.close()
            except OSError:
                pass


def deliver_pending(*, selected_id=None, clock=lambda: datetime.now(timezone.utc)):
    _, config = settings()
    if config is None:
        return
    claimed = claim(config, clock(), selected_id)
    if not claimed:
        return
    delivery_id, attempts, body = claimed
    status, reason = smtp_submit(config, body)
    next_at = None
    if status == "retry":
        if attempts <= len(RETRY_DELAYS):
            next_at = (clock() + timedelta(seconds=RETRY_DELAYS[attempts - 1])).isoformat()
        else:
            status = "failed"
    with ai_store.database() as db:
        db.execute("""UPDATE email_outbox SET status=?, reason=?, next_at=?
                      WHERE id=? AND status='sending'""", (status, reason, next_at, delivery_id))


def send_test():
    status, _ = settings()
    if status != "pending":
        return {"status": status}
    created = ai_store.now()
    delivery_id = "test-" + created[:10]
    with ai_store.database() as db:
        enqueue(db, delivery_id, created)
    deliver_pending(selected_id=delivery_id)
    with ai_store.database() as db:
        return dict(db.execute("SELECT status, attempts, reason FROM email_outbox WHERE id=?",
                               (delivery_id,)).fetchone())
