"""Private SQLite storage for investigations, approvals and effect reviews."""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from .settings import ROOT, private_dir

TASK_TAG = "сгенерирована ИИ-помощником"


def dumps(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)


def now():
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def database():
    path = (private_dir() / "ai.sqlite3").resolve()
    if path.is_relative_to(ROOT):
        raise ValueError("Укажите PM_PRIVATE_DIR за пределами рабочей копии")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=5, isolation_level=None)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("BEGIN IMMEDIATE")
        version = db.execute("PRAGMA user_version").fetchone()[0]
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
        elif version != 2:
            raise ValueError("Версия хранилища неизвестна")
        yield db
        db.commit()
    finally:
        db.close()


def save_run(report):
    with database() as db:
        db.execute("INSERT INTO runs VALUES (?, ?, ?)", (report["run_id"], now(), dumps(report)))


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
    target = finite(request.get("target")) if action == "accept" else None
    guardrail = request.get("control_min")
    if guardrail is not None:
        guardrail = finite(guardrail)
    fingerprint = hashlib.sha256(dumps([run_id, proposal_id, action, target, guardrail]).encode()).hexdigest()
    report = run(run_id)
    proposal = next((p for p in report["proposed_tasks"] if p["id"] == proposal_id), None)
    if proposal is None:
        raise ValueError("Предложение не найдено")
    plan = report["effect_plan"]
    if action == "accept" and (plan["baseline"] is None or target <= 0):
        raise ValueError("Для согласования нужны исходный уровень и положительная цель")
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
                effect_plan=plan | {"target": target, "control_min": guardrail},
                data_mode=report["data_mode"], redmine_status="not_configured",
                fingerprint=fingerprint, created=now(),
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
        result = dict(status="waiting_for_data", mode=mode, task_id=task_id, plan=plan)
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
            computed = effect(rows, observation["fix"], plan["target"])
            if "start" in computed:
                result["measurement"] = {k: v for k, v in computed.items() if k != "status"}
                core = computed["kaspersky"]
                control = computed["astra"]
                result["change_from_baseline"] = core - plan["baseline"]
                result["control_change"] = control - plan["control_baseline"] if plan["control_baseline"] is not None else None
                limit = plan["control_min"]
                if limit is not None and control < limit:
                    status = "inconclusive"
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
            "inconclusive": "Проверить контроль и качество данных",
        }[result["status"]]
        if observation and mode == "actual":
            fingerprint = hashlib.sha256(dumps(observation).encode()).hexdigest()
            db.execute("INSERT OR IGNORE INTO reviews VALUES (?, ?, ?, ?)",
                       (task_id, fingerprint, now(), dumps(result)))
        return result


def record_check(signature, status, delta):
    with database() as db:
        previous = db.execute("SELECT status FROM monitor WHERE signature=?", (signature,)).fetchone()
        notify = status == "signal" and (previous is None or previous["status"] != "signal")
        created = now()
        db.execute("""INSERT INTO monitor VALUES (?, ?, ?, ?)
                      ON CONFLICT(signature) DO UPDATE SET status=excluded.status,
                      changed=excluded.changed, delta=excluded.delta""",
                   (signature, status, created, delta))
    return dict(status=status, notification=notify, delta=delta, checked_at=created)


def public_report(run_id):
    report = run(run_id)
    return {
        "llm_status": report["llm_status"], "data_mode": report["data_mode"],
        "sources": {key: value["status"] for key, value in report["sources_status"].items()},
        "hypothesis_count": len(report["hypotheses"]),
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
    return {"status": "pending", "local_task_id": task["id"], "tag": TASK_TAG,
            "payload": {"issue": {"subject": task["title"], "description": description}}}
