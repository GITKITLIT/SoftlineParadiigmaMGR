"""Versioned product observations. SQL and write privileges stay in the backend."""
import hashlib
import json
import math
import re
from datetime import date, datetime, timezone
from urllib.parse import urlsplit

from . import ai_store

MODES = {"local", "synthetic", "demo_counter"}
STREAMS = {"sales", "metrika", "competitor", "funnel", "release"}
QUALITY = {"complete", "partial", "sampled", "missing", "snippet", "excerpt", "reported", "retracted"}
AGGREGATION = {"sum", "mean", "exact", "snapshot"}
SECRET = re.compile(
    r"(?i)(?:gh[pousr]_[a-z0-9_]{16,}|github_pat_[a-z0-9_]{16,}|"
    r"(?:sk-|xox[baprs]-)[a-z0-9_-]{16,}|AKIA[A-Z0-9]{16}|"
    r"(?:bearer|oauth) +[a-z0-9._~-]{12,}|"
    r"(?:password|passwd|api[_ -]?key|access[_ -]?token|пароль|token|secret)\s*[:=]\s*[^\s,;]{4,})"
)


def redact(text):
    return SECRET.sub("[скрыто]", text)


def safe_data(value):
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: safe_data(v) for k, v in value.items()
                if str(k).casefold() not in {"token", "password", "api_key", "authorization", "secret"}}
    if isinstance(value, (list, tuple)):
        return [safe_data(v) for v in value]
    return value


def stamp(value=None):
    if value is None:
        return ai_store.now()
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError("Укажите дату получения с часовым поясом")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Укажите дату получения с часовым поясом")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


def iso(value):
    if not isinstance(value, str) or len(value) != 10 or date.fromisoformat(value).isoformat() != value:
        raise ValueError("Дата должна быть в формате ГГГГ-ММ-ДД")
    return value


def label(value, limit=160):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or SECRET.search(value):
        raise ValueError("Некорректный идентификатор или название")
    return value.strip()


def digest(value):
    return hashlib.sha256(ai_store.dumps(value).encode()).hexdigest()


def migrate(db):
    statements = (
        """CREATE TABLE memory_sources (
            id INTEGER PRIMARY KEY, mode TEXT NOT NULL CHECK(mode IN ('local','synthetic','demo_counter')),
            stream TEXT NOT NULL, code TEXT NOT NULL, UNIQUE(mode,stream,code)
        )""",
        """CREATE TABLE memory_series (
            id INTEGER PRIMARY KEY, source_id INTEGER NOT NULL REFERENCES memory_sources(id),
            product TEXT NOT NULL, subject TEXT NOT NULL, metric TEXT NOT NULL, unit TEXT NOT NULL,
            method TEXT NOT NULL, segment TEXT NOT NULL, timezone TEXT NOT NULL, aggregation TEXT NOT NULL,
            dimensions TEXT NOT NULL,
            UNIQUE(source_id,product,subject,metric,unit,method,segment,timezone,aggregation,dimensions)
        )""",
        """CREATE TABLE memory_observations (
            id INTEGER PRIMARY KEY, series_id INTEGER NOT NULL REFERENCES memory_series(id),
            start TEXT NOT NULL, end TEXT NOT NULL CHECK(end>=start), event_date TEXT,
            retrieved TEXT NOT NULL, recorded TEXT NOT NULL, checked TEXT NOT NULL,
            version INTEGER NOT NULL CHECK(version>0), supersedes INTEGER REFERENCES memory_observations(id),
            number REAL, quality TEXT NOT NULL, reference TEXT, payload TEXT NOT NULL,
            fingerprint TEXT NOT NULL, UNIQUE(series_id,start,end,version)
        )""",
        "CREATE INDEX memory_observations_time ON memory_observations(series_id,start,end,version DESC)",
        "CREATE INDEX memory_observations_recorded ON memory_observations(recorded)",
        "CREATE INDEX memory_series_lookup ON memory_series(product,metric,source_id)",
        """CREATE TABLE memory_documents (
            id INTEGER PRIMARY KEY, collection TEXT NOT NULL UNIQUE
        )""",
        """CREATE TABLE memory_document_versions (
            id INTEGER PRIMARY KEY, document_id INTEGER NOT NULL REFERENCES memory_documents(id),
            revision INTEGER NOT NULL, captured TEXT NOT NULL, checked TEXT NOT NULL, fingerprint TEXT NOT NULL,
            UNIQUE(document_id,revision)
        )""",
        """CREATE TABLE memory_fragments (
            id INTEGER PRIMARY KEY, version_id INTEGER NOT NULL REFERENCES memory_document_versions(id),
            external_id TEXT NOT NULL, kind TEXT NOT NULL, reference TEXT NOT NULL, content TEXT NOT NULL,
            product TEXT, observed TEXT, metadata TEXT NOT NULL, UNIQUE(version_id,external_id)
        )""",
        "CREATE INDEX memory_fragments_versions ON memory_fragments(version_id)",
        """CREATE TABLE memory_run_evidence (
            run_id TEXT NOT NULL REFERENCES runs(id), evidence_id TEXT NOT NULL,
            observation_id INTEGER REFERENCES memory_observations(id),
            kind TEXT NOT NULL, origin TEXT NOT NULL, PRIMARY KEY(run_id,evidence_id)
        )""",
        """CREATE TABLE memory_hypotheses (
            run_id TEXT NOT NULL REFERENCES runs(id), proposal_id TEXT NOT NULL,
            cause TEXT NOT NULL, status TEXT NOT NULL CHECK(status='hypothesis'),
            PRIMARY KEY(run_id,proposal_id)
        )""",
        """CREATE TABLE memory_sync_jobs (
            id INTEGER PRIMARY KEY, kind TEXT NOT NULL, job_key TEXT NOT NULL,
            status TEXT NOT NULL, started TEXT NOT NULL, finished TEXT,
            lease TEXT, attempts INTEGER NOT NULL DEFAULT 1, detail TEXT, UNIQUE(kind,job_key)
        )""",
        """CREATE TABLE memory_audit (
            id INTEGER PRIMARY KEY, event TEXT NOT NULL, stamp TEXT NOT NULL, subject TEXT NOT NULL
        )""",
    )
    for statement in statements:
        db.execute(statement)
    try:
        db.execute("""CREATE VIRTUAL TABLE memory_fragment_fts USING fts5(
            content, reference, content='memory_fragments', content_rowid='id', tokenize='unicode61'
        )""")
    except Exception as error:
        if "no such module: fts5" not in str(error).lower():
            raise
    else:
        db.execute("""CREATE TRIGGER memory_fragment_index AFTER INSERT ON memory_fragments BEGIN
            INSERT INTO memory_fragment_fts(rowid,content,reference)
            VALUES (new.id,new.content,new.reference);
        END""")


def observation(db, row, *, recorded=None):
    mode, stream = row["mode"], row["stream"]
    if mode not in MODES or stream not in STREAMS:
        raise ValueError("Неподдерживаемый источник или режим")
    source = label(row["source"])
    product, metric, unit = (label(row[key]) for key in ("product", "metric", "unit"))
    subject = label(row.get("subject") or "-")
    method, zone = (label(row.get(key) or fallback) for key, fallback in (
        ("method", "unspecified"), ("timezone", "UTC"),
    ))
    segment = label(row.get("segment") or "all")
    aggregation = row.get("aggregation", "exact")
    quality = row.get("quality", "reported")
    if aggregation not in AGGREGATION or quality not in QUALITY:
        raise ValueError("Некорректный вид агрегации или качество")
    start, end = iso(row["start"]), iso(row.get("end") or row["start"])
    event_date = iso(row["event_date"]) if row.get("event_date") else None
    if end < start:
        raise ValueError("Конец наблюдения предшествует началу")
    number = row.get("value")
    if number is not None and (type(number) not in (int, float) or not math.isfinite(number)):
        raise ValueError("Показатель должен быть числом или null")
    if number is None and quality == "complete" and aggregation != "snapshot":
        quality = "missing"
    reference = redact(str(row["reference"]))[:700] if row.get("reference") else None
    payload = ai_store.dumps(safe_data(row.get("metadata", {})))
    dims = ai_store.dumps(safe_data(row.get("dimensions", {})))
    if len(payload) > 12_000 or len(dims) > 1500:
        raise ValueError("Слишком большой объём метаданных")
    retrieved = stamp(row.get("retrieved"))
    clock = recorded or ai_store.now()
    db.execute("INSERT OR IGNORE INTO memory_sources(mode,stream,code) VALUES (?,?,?)", (mode, stream, source))
    source_id = db.execute("SELECT id FROM memory_sources WHERE mode=? AND stream=? AND code=?",
                           (mode, stream, source)).fetchone()[0]
    key = (source_id, product, subject, metric, unit, method, segment, zone, aggregation, dims)
    db.execute("""INSERT OR IGNORE INTO memory_series
                  (source_id,product,subject,metric,unit,method,segment,timezone,aggregation,dimensions)
                  VALUES (?,?,?,?,?,?,?,?,?,?)""", key)
    series_id = db.execute("""SELECT id FROM memory_series WHERE source_id=? AND product=? AND subject=?
                  AND metric=? AND unit=? AND method=? AND segment=? AND timezone=? AND aggregation=? AND dimensions=?""",
                           key).fetchone()[0]
    signature = digest([number, event_date, quality, reference, payload])
    earlier = db.execute("""SELECT id,version,fingerprint,retrieved FROM memory_observations WHERE
                           series_id=? AND start=? AND end=? ORDER BY version DESC LIMIT 1""",
                         (series_id, start, end)).fetchone()
    if earlier and signature == earlier["fingerprint"]:
        db.execute("UPDATE memory_observations SET checked=? WHERE id=?", (clock, earlier["id"]))
        return earlier["id"], False
    if earlier and retrieved < earlier["retrieved"] and not row.get("correction"):
        raise ValueError("Для исправления версии с более старой датой требуется явное подтверждение")
    version = earlier["version"] + 1 if earlier else 1
    saved = db.execute("""INSERT INTO memory_observations
        (series_id,start,end,event_date,retrieved,recorded,checked,version,supersedes,
         number,quality,reference,payload,fingerprint)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (series_id, start, end, event_date, retrieved, clock, clock, version,
         earlier["id"] if earlier else None, number, quality, reference, payload, signature))
    if version > 1:
        db.execute("INSERT INTO memory_audit(event,stamp,subject) VALUES (?,?,?)",
                   ("observation_revision", clock, str(saved.lastrowid)))
    return saved.lastrowid, True


def write_observations(rows):
    if not isinstance(rows, list) or len(rows) > 10_000:
        raise ValueError("В одной партии допускается до 10000 наблюдений")
    with ai_store.database() as db:
        ids = [observation(db, row) for row in rows]
    return {"created": sum(created for _, created in ids), "ids": [ref for ref, _ in ids]}


def document_version(db, collection, items):
    normalized = safe_data(items)
    signature = digest(normalized)
    clock = ai_store.now()
    db.execute("INSERT OR IGNORE INTO memory_documents(collection) VALUES (?)", (collection,))
    doc = db.execute("SELECT id FROM memory_documents WHERE collection=?", (collection,)).fetchone()[0]
    last = db.execute("""SELECT id,revision,fingerprint FROM memory_document_versions WHERE document_id=?
                         ORDER BY revision DESC LIMIT 1""", (doc,)).fetchone()
    if last and last["fingerprint"] == signature:
        db.execute("UPDATE memory_document_versions SET checked=? WHERE id=?", (clock, last["id"]))
        return normalized
    version = db.execute("""INSERT INTO memory_document_versions(document_id,revision,captured,checked,fingerprint)
                            VALUES (?,?,?,?,?)""",
                         (doc, last["revision"] + 1 if last else 1, clock, clock, signature)).lastrowid
    db.executemany("""INSERT INTO memory_fragments
        (version_id,external_id,kind,reference,content,product,observed,metadata) VALUES (?,?,?,?,?,?,?,?)""",
        [(version, item["id"], item.get("kind", "document"), item.get("ref", ""), item.get("text", ""),
          item.get("product"), item.get("observed_at"), ai_store.dumps(item.get("locator", {})))
         for item in normalized])
    return normalized


def record_web(db, item):
    now = item.get("retrieved_at") or ai_store.now()
    url = item.get("locator")
    host = urlsplit(url or "").hostname
    if not host or urlsplit(url).scheme not in {"https", "http"}:
        raise ValueError("Нужна ссылка на публичный источник")
    offer = item.get("offer") if isinstance(item.get("offer"), dict) else {}
    try:
        published = stamp(item["observed_at"])[:10] if item.get("observed_at") else None
    except ValueError:
        published = None
    return observation(db, {
        "mode": "synthetic" if item.get("data_mode") == "synthetic" else "local",
        "stream": "competitor", "source": "yandex_search", "product": item.get("product") or "public_catalogue",
        "subject": item.get("competitor") or host, "metric": offer.get("metric", "offer"),
        "unit": offer.get("currency") or "text", "value": offer.get("value"), "aggregation": "snapshot",
        "quality": "snippet" if item.get("quality") == "snippet" else "excerpt",
        "method": "v1", "start": stamp(now)[:10], "event_date": published,
        "retrieved": now, "reference": url,
        "dimensions": {"topic": item.get("topic"), "terms": offer.get("terms"), "page": digest(url)},
        "metadata": {"evidence_id": item["id"], "title": item.get("ref"), "excerpt": item.get("text"),
                     "publication_date": published, "content_type": item.get("quality")},
    })


def record_investigation(db, report):
    for item in report.get("evidence", []):
        origin = "document" if item["kind"] in {"interview", "competitor_local", "research"} else "observation"
        observation_id = item.get("memory_observation_id")
        if item["kind"] == "competitor_web":
            observation_id, _ = record_web(db, item)
        if item["kind"] == "memory_context":
            origin = "historical_context"
        db.execute("""INSERT OR IGNORE INTO memory_run_evidence
                       (run_id,evidence_id,observation_id,kind,origin) VALUES (?,?,?,?,?)""",
                   (report["run_id"], item["id"], observation_id, item["kind"], origin))
    db.executemany("INSERT OR IGNORE INTO memory_hypotheses VALUES (?,?,?,'hypothesis')",
                   [(report["run_id"], item["id"], item["hypothesis"])
                    for item in report.get("proposed_tasks", [])])


def find_fragments(db, query, *, product=None, kind=None, start=None, end=None, limit=12):
    words = re.findall(r"[^\W_]{2,}", query.casefold())[:12]
    if not words:
        return []
    limit = max(1, min(limit, 25))
    where, args = [], []
    for column, op, value in (("product", "=", product), ("kind", "=", kind),
                               ("observed", ">=", start), ("observed", "<=", end)):
        if value:
            where.append(f"f.{column} {op} ?")
            args.append(value)
    predicates = "".join(" AND " + clause for clause in where)
    fields = """f.id,f.kind,f.reference,f.content,f.product,f.observed,f.metadata,
                v.revision,v.captured"""
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='memory_fragment_fts'").fetchone():
        return [dict(row) for row in db.execute(f"""
            SELECT {fields} FROM memory_fragment_fts JOIN memory_fragments f ON f.id=memory_fragment_fts.rowid
            JOIN memory_document_versions v ON f.version_id=v.id
            WHERE memory_fragment_fts MATCH ? {predicates}
            ORDER BY rank LIMIT ?""", [" OR ".join(f'"{word}"*' for word in words), *args, limit])]
    matches = " OR ".join("f.content LIKE ? ESCAPE '!'" for _ in words)
    return [dict(row) for row in db.execute(f"""
        SELECT {fields} FROM memory_fragments f JOIN memory_document_versions v ON f.version_id=v.id
        WHERE ({matches}) {predicates} ORDER BY f.id DESC LIMIT ?""",
        [*(f"%{word}%" for word in words), *args, limit])]
