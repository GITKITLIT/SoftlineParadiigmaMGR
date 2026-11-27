"""Validate the configuration delivered by systemd without echoing its values."""
import importlib.util
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from app import page
from pm import ai_store, collection, monitor, qwen
from pm.analysis_context import allowed_domain, allowlist

DB = Path("/var/lib/paradiigma/db")
DOCS = Path("/var/lib/paradiigma/documents")
BACKUP_STATUS = "/var/lib/paradiigma-backup-status/status.json"
INPUTS = ("PM_WORKBOOK", "PM_CASE_CONFIG", "PM_SNAPSHOT", "PM_EVIDENCE", "PM_BENCHMARK", "PM_LLM_PROMPT_FILE")


def require(condition, label):
    if not condition:
        raise ValueError(label)


def validate():
    env = os.environ
    mode = env.get("PARADIIGMA_MODE")
    require(mode in {"demo", "private"}, "PARADIIGMA_MODE")
    require(env.get("PM_PRIVATE_DIR") == str(DB) and DB.is_dir() and not DB.is_symlink(), "PM_PRIVATE_DIR")
    for key in ("PM_BACKUP_STATUS_FILE", "PM_MIGRATION_STATUS_FILE"):
        require(env.get(key, "") in {"", BACKUP_STATUS}, key)
    for key in INPUTS:
        if env.get(key):
            path = Path(env[key])
            require(mode == "private" and path.is_absolute() and path.resolve().is_relative_to(DOCS)
                    and path.is_file() and os.access(path, os.R_OK), key)
    counter, token = env.get("METRIKA_COUNTER_ID", ""), env.get("METRIKA_TOKEN", "")
    require((not counter and not token) or
            (mode == "private" and counter.isascii() and counter.isdigit() and token), "METRIKA_COUNTER_ID / METRIKA_TOKEN")
    require(env.get("PM_COLLECT_METRIKA", "false").lower() in {"false", "true"}, "PM_COLLECT_METRIKA")
    require(env.get("PM_COLLECT_METRIKA", "").lower() != "true" or token, "PM_COLLECT_METRIKA")
    require(env.get("METRIKA_SCOPE", "demo_counter") in {"demo_counter", "store_counter"}, "METRIKA_SCOPE")
    search = env.get("YANDEX_SEARCH_ENABLED", "false").lower()
    require(search in {"false", "true"}, "YANDEX_SEARCH_ENABLED")
    email = env.get("PM_EMAIL_ENABLED", "false").strip().lower()
    require(email in {"false", "true"}, "PM_EMAIL_ENABLED")
    if mode == "demo":
        require(search == "false" and not any(env.get(k) for k in
                ("YANDEX_SEARCH_FOLDER_ID", "YANDEX_SEARCH_API_KEY", "YANDEX_SEARCH_IAM_TOKEN")), "demo search configuration")
        require(email == "false" and not any(env.get(k) for k in
                ("PM_SMTP_HOST", "PM_SMTP_USERNAME", "PM_SMTP_PASSWORD", "PM_EMAIL_FROM", "PM_EMAIL_TO")),
                "demo SMTP configuration")
    if email == "true":
        require(importlib.util.find_spec("pm.email_notifications") is not None, "pm.email_notifications")
        from pm import email_notifications
        require(email_notifications.settings()[0] == "pending", "SMTP configuration")
    if search == "true":
        require(importlib.util.find_spec("pm.web_search") is not None, "pm.web_search")
        from pm.yandex_search.config import Config
        from pm.yandex_search.page_policy import PageConfig
        config = Config.from_env()
        require(config.validate_credentials() is None, "YANDEX_SEARCH credentials")
        PageConfig.from_env(config.domains)
        domains = allowlist("YANDEX_SEARCH_ALLOWED_DOMAINS")
        require(domains and all(allowed_domain(d) for d in domains), "YANDEX_SEARCH_ALLOWED_DOMAINS")
        require(allowlist("YANDEX_SEARCH_PUBLIC_PRODUCTS"), "YANDEX_SEARCH_PUBLIC_PRODUCTS")
    for key in ("METRIKA_GOALS", "METRIKA_SEGMENTS", "METRIKA_EXTRA_METRICS"):
        require(isinstance(json.loads(env.get(key, "{}")), dict), key)
    qwen.endpoint()
    collection.interval()
    monitor.interval()
    monitor.max_daily()
    # Validate the same inputs used to build the landing page without writing them.
    page()
    with ai_store.database() as db:
        require(db.execute("SELECT 1").fetchone()[0] == 1, "SQLite")


if __name__ == "__main__":
    try:
        validate()
    except Exception:
        # Paths, parser exceptions and credentials must stay out of journald.
        print("Preflight failed. Check app.env, input permissions and SQLite schema.", flush=True)
        sys.exit(1)
