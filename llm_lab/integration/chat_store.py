"""Opt-in conversation tables in AI Core's existing SQLite transaction."""

import json
from uuid import uuid4

from pm import ai_store
from pm.memory import safe_data

HISTORY_LIMIT = 30
NOT_FOUND = "Диалог не найден."


def database():
    return ai_store.database()


def install():
    """Called once before the integration routes accept requests."""
    with database() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS qwen_dialogs (
            id TEXT PRIMARY KEY, owner TEXT NOT NULL, updated TEXT NOT NULL,
            version INTEGER NOT NULL DEFAULT 0
        )""")
        db.execute("""CREATE INDEX IF NOT EXISTS qwen_dialogs_owner
            ON qwen_dialogs(owner, updated DESC)""")
        db.execute("""CREATE TABLE IF NOT EXISTS qwen_turns (
            dialog TEXT NOT NULL REFERENCES qwen_dialogs(id) ON DELETE CASCADE,
            position INTEGER NOT NULL, body TEXT NOT NULL,
            PRIMARY KEY(dialog, position)
        )""")


def create(owner):
    identifier = uuid4().hex
    with database() as db:
        db.execute("INSERT INTO qwen_dialogs(id, owner, updated) VALUES (?, ?, ?)",
                   (identifier, owner, ai_store.now()))
    return identifier


def dialogs(owner):
    with database() as db:
        rows = db.execute("""SELECT d.id, d.updated, json_extract(t.body, '$.question') AS label
                             FROM qwen_dialogs AS d LEFT JOIN qwen_turns AS t
                             ON t.dialog=d.id AND t.position=d.version
                             WHERE d.owner=? ORDER BY d.updated DESC LIMIT 30""", (owner,))
        return [dict(row) for row in rows]


def state(owner, identifier):
    with database() as db:
        row = db.execute("""SELECT d.version, t.body FROM qwen_dialogs AS d LEFT JOIN qwen_turns AS t
                            ON d.id=t.dialog AND d.version=t.position WHERE d.id=? AND d.owner=?""",
                         (identifier, owner)).fetchone()
        if row is None:
            raise ValueError(NOT_FOUND)
        return row["version"], json.loads(row["body"]) if row["body"] else None


def history(owner, identifier):
    with database() as db:
        if db.execute("SELECT 1 FROM qwen_dialogs WHERE id=? AND owner=?",
                      (identifier, owner)).fetchone() is None:
            raise ValueError(NOT_FOUND)
        turns = db.execute("""SELECT position, body FROM qwen_turns WHERE dialog=?
                              ORDER BY position DESC LIMIT ?""", (identifier, HISTORY_LIMIT)).fetchall()
    result = []
    for row in reversed(turns):
        turn = json.loads(row["body"])
        result.append({"position": row["position"], "question": turn["question"],
                       "answer": turn["reply"]["answer"]})
    return result


def get_turn(owner, identifier, position):
    with database() as db:
        row = db.execute("""SELECT t.body FROM qwen_turns AS t JOIN qwen_dialogs AS d ON d.id=t.dialog
                            WHERE t.dialog=? AND d.owner=? AND t.position=?""",
                         (identifier, owner, position)).fetchone()
        if row is None:
            raise ValueError("Сообщение не найдено.")
    return json.loads(row["body"])


def append(owner, identifier, version, turn):
    position = version + 1
    with database() as db:
        changed = db.execute("""UPDATE qwen_dialogs SET version=?, updated=?
                                WHERE id=? AND owner=? AND version=?""",
                             (position, ai_store.now(), identifier, owner, version)).rowcount
        if changed != 1:
            raise ValueError("Диалог уже обновился. Загрузите сообщения и повторите вопрос.")
        db.execute("INSERT INTO qwen_turns VALUES (?, ?, ?)",
                   (identifier, position, ai_store.dumps(safe_data(turn))))
    return position
