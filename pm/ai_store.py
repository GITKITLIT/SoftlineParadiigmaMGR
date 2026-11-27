"""Private SQLite storage for investigations, approvals and effect reviews."""
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from .settings import private_dir

TASK_TAG = "сгенерирована ИИ-помощником"


def dumps(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)


def now():
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def database():
    path = private_dir() / "ai.sqlite3"
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=5, isolation_level=None)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("BEGIN IMMEDIATE")
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1, 2, 3, 4, 5):
            raise ValueError("Версия хранилища неизвестна")
        if version == 0:
            db.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, created TEXT NOT NULL, report TEXT NOT NULL)")
            db.execute("""CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
                proposal_id TEXT NOT NULL, action TEXT NOT NULL, body TEXT NOT NULL,
                UNIQUE(run_id, proposal_id))""")
            db.execute("""CREATE TABLE IF NOT EXISTS idempotency (
                key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, task_id TEXT NOT NULL REFERENCES tasks(id))""")
            db.execute("""CREATE TABLE IF NOT EXISTS reviews (
                task_id TEXT NOT NULL REFERENCES tasks(id), fingerprint TEXT NOT NULL, created TEXT NOT NULL,
                body TEXT NOT NULL, PRIMARY KEY(task_id, fingerprint))""")
            db.execute("PRAGMA user_version=1")
        if version < 2:
            db.execute("""CREATE TABLE IF NOT EXISTS monitor (
                signature TEXT PRIMARY KEY, status TEXT NOT NULL, changed TEXT NOT NULL, delta REAL)""")
            db.execute("PRAGMA user_version=2")
        if version < 3:
            db.execute("""CREATE TABLE IF NOT EXISTS investigation_queue (
                id TEXT PRIMARY KEY, signature TEXT NOT NULL, created TEXT NOT NULL,
                status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, lease TEXT,
                run_id TEXT REFERENCES runs(id)
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS queue_status ON investigation_queue(status, created)")
            db.execute("PRAGMA user_version=3")
        if version < 4:
            db.execute("""CREATE TABLE IF NOT EXISTS imported_evidence (
                id TEXT PRIMARY KEY, collection TEXT NOT NULL, body TEXT NOT NULL
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS evidence_collection ON imported_evidence(collection)")
            db.execute("PRAGMA user_version=4")
        if version < 5:
            from .memory import migrate, document_version
            migrate(db)
            collections = db.execute("SELECT DISTINCT collection FROM imported_evidence").fetchall()
            for row in collections:
                items = [json.loads(item["body"]) for item in db.execute(
                    "SELECT body FROM imported_evidence WHERE collection=? ORDER BY rowid", (row["collection"],))]
                document_version(db, row["collection"], items)
            db.execute("PRAGMA user_version=5")
        yield db
        db.commit()
    finally:
        db.close()



def store_excerpts(collection, items):
    from .memory import document_version
    with database() as db:
        items = document_version(db, collection, items)
        db.execute("DELETE FROM imported_evidence WHERE collection=?", (collection,))
        db.executemany("""INSERT INTO imported_evidence VALUES (?, ?, ?)
                          ON CONFLICT(id) DO UPDATE SET collection=excluded.collection, body=excluded.body""",
                       [(row["id"], collection, dumps(row)) for row in items])
    return len(items)


def stored_excerpts():
    path = private_dir() / "ai.sqlite3"
    if not path.exists():
        return []
    with database() as db:
        return [json.loads(row["body"]) for row in db.execute("SELECT body FROM imported_evidence")]

def save_run(report):
    from .memory import record_investigation, safe_data
    report = safe_data(report)
    with database() as db:
        db.execute("INSERT INTO runs VALUES (?, ?, ?)", (report["run_id"], now(), dumps(report)))
        record_investigation(db, report)


def run(run_id):
    with database() as db:
        row = db.execute("SELECT report FROM runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            raise ValueError("Расследование не найдено")
        return json.loads(row[0])


def recent():
    with database() as db:
        rows = db.execute("SELECT id, created, report FROM runs ORDER BY created DESC LIMIT 20")
        return [
            dict(run_id=row["id"], created=row["created"], llm_status=report["llm_status"],
                 data_mode=report["data_mode"], trace=report["trace"], usage=report.get("usage", {}))
            for row in rows for report in [json.loads(row["report"])]
        ]


def decide(request):
    import hashlib
    from uuid import uuid4
    from .analytics import finite

    run_id, proposal_id, action, key = (
        request.get(name) for name in ("run_id", "proposal_id", "action", "idempotency_key")
    )
    if (action not in ("accept", "reject") or any(
        not isinstance(value, str) or not 1 <= len(value) <= 120
        for value in (run_id, proposal_id, key)
    )):
        raise ValueError("Нужны корректное действие, идентификаторы и ключ повтора")
    from .memory import redact
    reason = request.get("reason")
    if reason is not None and (not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 800):
        raise ValueError("Укажите краткую причину решения")
    reason = redact(reason.strip()) if reason else None
    target = finite(request.get("target")) if action == "accept" else None
    guardrail = request.get("control_min")
    if guardrail is not None:
        guardrail = finite(guardrail)
    guards = request.get("guardrail_metrics") or {}
    if not isinstance(guards, dict) or len(guards) > 5 or any(
        not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", name)
        for name in guards
    ):
        raise ValueError("Передайте до пяти именованных контрольных показателей")
    guards = {name: finite(value) for name, value in guards.items()}
    if guards and action != "accept":
        raise ValueError("Контрольные показатели задаются при согласовании")
    stop_if_below = request.get("stop_if_below")
    if stop_if_below is not None:
        stop_if_below = finite(stop_if_below)
        if action != "accept" or stop_if_below > target:
            raise ValueError("Порог остановки должен быть не выше цели")
    report = run(run_id)
    proposal = next((p for p in report["proposed_tasks"] if p["id"] == proposal_id), None)
    if proposal is None:
        raise ValueError("Предложение не найдено")
    plan = proposal.get("plan") or report["effect_plan"]
    if action == "accept" and not proposal.get("backlog_verified", True):
        raise ValueError("Сначала проверьте существующий бэклог")
    if action == "accept" and (plan["baseline"] is None or target <= 0):
        raise ValueError("Для согласования нужны исходный уровень и положительная цель")
    identity = [run_id, proposal_id, action, target, guardrail]
    if stop_if_below is not None or guards:
        identity.extend([stop_if_below, guards])
    if reason:
        identity.append({"reason": reason})
    fingerprint = hashlib.sha256(dumps(identity).encode()).hexdigest()
    with database() as db:
        previous = db.execute("SELECT fingerprint, task_id FROM idempotency WHERE key=?", (key,)).fetchone()
        if previous and previous["fingerprint"] != fingerprint:
            raise ValueError("Ключ повтора уже связан с другим решением")
        saved = db.execute("SELECT body FROM tasks WHERE run_id=? AND proposal_id=?", (run_id, proposal_id)).fetchone()
        if saved:
            record = json.loads(saved[0])
            if record["fingerprint"] != fingerprint:
                raise ValueError("Предложение уже рассмотрено")
        else:
            record = proposal | dict(
                id=uuid4().hex, run_id=run_id, proposal_id=proposal_id, decision=action,
                effect_plan=plan | {"target": target, "control_min": guardrail, "stop_if_below": stop_if_below,
                                    "guardrail_metrics": guards},
                data_mode=report["data_mode"], redmine_status="not_configured",
                fingerprint=fingerprint, created=now(), decision_reason=reason,
            )
            db.execute("INSERT INTO tasks VALUES (?, ?, ?, ?, ?)",
                       (record["id"], run_id, proposal_id, action, dumps(record)))
        db.execute("INSERT OR IGNORE INTO idempotency VALUES (?, ?, ?)", (key, fingerprint, record["id"]))
        return {k: v for k, v in record.items() if k != "fingerprint"}


def task_list():
    with database() as db:
        rows = db.execute("""SELECT t.body, (
            SELECT r.body FROM reviews AS r WHERE r.task_id=t.id
            ORDER BY r.created DESC LIMIT 1
        ) AS last_review FROM tasks AS t ORDER BY t.rowid DESC LIMIT 50""")
        return [
            {k: v for k, v in json.loads(row["body"]).items() if k != "fingerprint"} |
            {"latest_review": json.loads(row["last_review"]) if row["last_review"] else None}
            for row in rows
        ]


def accepted_task(task_id):
    if not isinstance(task_id, str) or len(task_id) > 120:
        raise ValueError("Некорректный идентификатор задачи")
    with database() as db:
        row = db.execute("""SELECT t.body, (
            SELECT r.body FROM reviews AS r WHERE r.task_id=t.id
            ORDER BY r.created DESC LIMIT 1
        ) AS last_review FROM tasks t WHERE t.id=? AND t.action='accept'""", (task_id,)).fetchone()
    if not row:
        return None
    return json.loads(row["body"]) | {"latest_review": json.loads(row["last_review"]) if row["last_review"] else None}


def review(request):
    import hashlib
    from .analytics import WEEK, day, effect

    task_id = request.get("task_id")
    if not isinstance(task_id, str):
        raise ValueError("Укажите согласованную задачу")
    observation = request.get("observation")
    mode = request.get("mode", "actual")
    if mode not in ("actual", "simulation"):
        raise ValueError("Неизвестный режим оценки")
    with database() as db:
        row = db.execute("SELECT body FROM tasks WHERE id=? AND action='accept'", (task_id,)).fetchone()
        if not row:
            raise ValueError("Задача не согласована")
        task = json.loads(row[0])
        plan = task["effect_plan"]
        result = dict(status="waiting_for_data", mode=mode, task_id=task_id, plan=plan,
                      hypothesis=task["hypothesis"], limitations=[])
        if observation:
            if not isinstance(observation, dict) or not isinstance(observation.get("rows"), list):
                raise ValueError("Передайте окно оценки и массив недель")
            if mode == "actual" and task["data_mode"] != "local":
                raise ValueError("Для синтетического расследования доступна симуляция")
            if mode == "actual" and day(observation["fix"]) < day(task["created"]):
                raise ValueError("Дата исправления предшествует согласованию")
            rows = observation["rows"]
            if mode == "actual":
                today = datetime.now(timezone.utc).date()
                if any(row.get("complete") is True and day(row["week"]) + WEEK > today for row in rows):
                    raise ValueError("Полная неделя должна закончиться до даты проверки")
            factors = observation.get("external_factors", [])
            if (not isinstance(factors, list) or len(factors) > 8 or
                    any(not isinstance(item, str) or not 1 <= len(item) <= 180 for item in factors)):
                raise ValueError("Внешние факторы передаются списком из коротких описаний")
            result["release_date"] = day(observation["fix"]).isoformat()
            from .memory import safe_data
            result["external_factors"] = safe_data(factors)
            computed = effect(rows, observation["fix"], plan["target"], allow_missing_values=True)
            if "start" in computed:
                result["measurement"] = {k: v for k, v in computed.items() if k != "status"}
                core = computed["kaspersky"]
                control = computed["astra"]
                result["change_from_baseline"] = core - plan["baseline"]
                result["control_change"] = (
                    control - plan["control_baseline"]
                    if control is not None and plan["control_baseline"] is not None else None
                )
                if result["control_change"] is None:
                    result["limitations"].append("Контрольное сравнение недоступно.")
                elif plan["baseline"]:
                    before_core = (core / plan["baseline"] - 1) * 100
                    before_control = plan["control_baseline"]
                    if before_control:
                        result["control_adjusted_change_pp"] = before_core - (control / before_control - 1) * 100
                limit = plan["control_min"]
                stop = plan.get("stop_if_below")
                result["side_effects"] = compare_guardrails(plan, observation, computed)
                blocked = any(item["status"] != "ok" for item in result["side_effects"])
                if factors or blocked or (limit is not None and (control is None or control < limit)):
                    status = "inconclusive"
                elif stop is not None and core < stop:
                    status = "stop_condition_met"
                elif core >= plan["target"]:
                    status = "achieved"
                elif core > plan["baseline"]:
                    status = "partial"
                else:
                    status = "not_achieved"
                result["status"] = status
        result["recommendation"] = {
            "waiting_for_data": "Дождаться четырёх полных недель",
            "achieved": "Рассмотреть закрытие после проверки",
            "partial": "Доработать",
            "not_achieved": "Пересмотреть гипотезу",
            "inconclusive": "Проверить контроль, внешние факторы и качество данных",
            "stop_condition_met": "Рассмотреть отказ от дальнейшей разработки по согласованному порогу",
        }[result["status"]]
        if observation and mode == "actual":
            fingerprint = hashlib.sha256(dumps(observation).encode()).hexdigest()
            db.execute("INSERT OR IGNORE INTO reviews VALUES (?, ?, ?, ?)",
                       (task_id, fingerprint, now(), dumps(result)))
        return result


def compare_guardrails(plan, observation, measurement):
    from .analytics import finite

    submitted = observation.get("guardrails") or {}
    if not isinstance(submitted, dict):
        raise ValueError("Контрольные наблюдения передаются объектом")
    results = []
    for name, minimum in plan.get("guardrail_metrics", {}).items():
        item = submitted.get(name)
        row = dict(metric=name, minimum=minimum, status="missing", baseline=None, after=None)
        if isinstance(item, dict) and (item.get("start"), item.get("end")) == (measurement["start"], measurement["end"]):
            baseline = item.get("baseline")
            after = item.get("after")
            if after is not None:
                row.update(after=finite(after), baseline=finite(baseline) if baseline is not None else None,
                           status="ok" if after >= minimum else "below_minimum")
        results.append(row)
    return results


def record_check(signature, status, delta, *, enqueue=False):
    with database() as db:
        previous = db.execute("SELECT status FROM monitor WHERE signature=?", (signature,)).fetchone()
        notify = status == "signal" and (previous is None or previous["status"] != "signal")
        created = now()
        db.execute("""INSERT INTO monitor VALUES (?, ?, ?, ?)
                      ON CONFLICT(signature) DO UPDATE SET status=excluded.status,
                      changed=excluded.changed, delta=excluded.delta""",
                   (signature, status, created, delta))
        if notify and enqueue:
            from uuid import uuid4
            db.execute("INSERT INTO investigation_queue(id, signature, created, status) VALUES (?, ?, ?, 'pending')",
                       (uuid4().hex, signature, created))
    return dict(status=status, notification=notify, delta=delta, checked_at=created)



def claim_investigation(signature, max_daily):
    from datetime import timedelta

    current = datetime.now(timezone.utc)
    start = current.date().isoformat()
    with database() as db:
        db.execute("""UPDATE investigation_queue SET status='expired'
                      WHERE status='pending' AND (signature != ? OR created < ?)""",
                   (signature, start))
        db.execute("""UPDATE investigation_queue SET status='failed'
                      WHERE status='running' AND lease <= ?""", (current.isoformat(),))
        total = db.execute("""SELECT COALESCE(SUM(attempts), 0) FROM investigation_queue
                              WHERE created >= ?""", (start,)).fetchone()[0]
        running = db.execute("SELECT 1 FROM investigation_queue WHERE status='running' LIMIT 1").fetchone()
        if total >= max_daily or running:
            return None
        row = db.execute("""SELECT id FROM investigation_queue WHERE signature=?
                            AND status='pending' ORDER BY created LIMIT 1""", (signature,)).fetchone()
        if row:
            lease = (current + timedelta(minutes=5)).isoformat()
            db.execute("""UPDATE investigation_queue SET status='running', lease=?, attempts=attempts+1
                          WHERE id=?""", (lease, row["id"]))
            return row["id"]
        return None


def complete_investigation(notification_id, run_id):
    with database() as db:
        db.execute("""UPDATE investigation_queue SET status=?, run_id=?, lease=NULL
                      WHERE id=? AND status='running'""",
                   ("ready" if run_id else "failed", run_id, notification_id))


def notifications():
    with database() as db:
        return [dict(row) for row in db.execute("""
            SELECT id, created, status, run_id FROM investigation_queue
            ORDER BY created DESC LIMIT 20
        """)]


def daily_digest():
    """UTC daily view with independent local and synthetic buckets."""
    start = datetime.now(timezone.utc).date().isoformat()
    with database() as db:
        daily = {"local": {"runs": 0, "with_hypotheses": 0, "pending_review": 0, "recent_ids": []},
                 "synthetic": {"runs": 0, "with_hypotheses": 0, "pending_review": 0, "recent_ids": []}}
        rows = db.execute("""
            SELECT runs.id, runs.report, COUNT(tasks.id) AS reviews FROM runs
            LEFT JOIN tasks ON tasks.run_id=runs.id WHERE runs.created >= ?
            GROUP BY runs.id ORDER BY runs.created DESC LIMIT 100
        """, (start,))
        for row in rows:
            report = json.loads(row["report"])
            group = daily.setdefault(report["data_mode"], {
                "runs": 0, "with_hypotheses": 0, "pending_review": 0, "recent_ids": [],
            })
            group["runs"] += 1
            group["with_hypotheses"] += bool(report["hypotheses"])
            group["pending_review"] += len(report["proposed_tasks"]) > row["reviews"]
            if len(group["recent_ids"]) < 5:
                group["recent_ids"].append(row["id"])
        return {"date_utc": start, "groups": daily, "limit": 100}

def public_report(run_id):
    report = run(run_id)
    return {
        "llm_status": report["llm_status"], "data_mode": report["data_mode"],
        "sources": {key: value["status"] for key, value in report["sources_status"].items()},
        **({"hypothesis_count": len(report["hypotheses"])}
           if report["data_mode"] == "synthetic"
           and not any(item["data_mode"] in {"local", "store_counter"} for item in report["evidence"])
           else {}),
    }


def redmine_export(task_id):
    with database() as db:
        row = db.execute("SELECT body FROM tasks WHERE id=? AND action='accept'", (task_id,)).fetchone()
        if row is None:
            raise ValueError("Задача не согласована")
        task = json.loads(row[0])
    plan = task["effect_plan"]
    description = "\n".join([
        "Гипотеза · " + task["hypothesis"], "Проверка · " + task["verification"],
        "KPI · " + plan["primary_kpi"], "Цель · " + str(plan["target"]),
        "Тег · " + TASK_TAG,
    ])
    existing = task.get("existing_tasks") or []
    if task.get("action") == "review_existing_priority" and existing:
        return {"status": "pending", "action": "review_existing_priority",
                "local_task_id": task["id"], "tag": TASK_TAG,
                "existing_task_ids": [item["id"] for item in existing], "assessment": description}
    return {"status": "pending", "local_task_id": task["id"], "tag": TASK_TAG,
            "payload": {"issue": {"subject": task["title"], "description": description}}}
