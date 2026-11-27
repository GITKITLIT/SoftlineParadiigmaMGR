"""Deterministic window comparisons and a small, read-only Product Memory API."""
import json
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import ai_store, memory

MAX_WINDOW = 1096
JOIN = """FROM memory_series s JOIN memory_sources x ON x.id=s.source_id"""


def require_mode(mode):
    if mode not in memory.MODES:
        raise ValueError("Выберите режим local или synthetic")
    return mode


def window(value):
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError("Укажите начало и конец периода")
    start, end = map(memory.iso, value)
    length = (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
    if not 1 <= length <= MAX_WINDOW:
        raise ValueError("Период должен содержать от 1 до 1096 дней")
    return start, end, length


def series_index(mode, *, product=None):
    require_mode(mode)
    with ai_store.database() as db:
        rows = db.execute(f"""SELECT s.id,s.product,s.subject,s.metric,s.unit,s.method,s.segment,s.timezone,
                     s.aggregation,s.dimensions,x.stream,x.code AS source
                     {JOIN} WHERE x.mode=? AND (? IS NULL OR s.product=?)
                     ORDER BY s.product,x.stream,s.metric LIMIT 200""", (mode, product, product))
        return [dict(row) | {"dimensions": json.loads(row["dimensions"])} for row in rows]


def series_for(db, mode, series_id):
    require_mode(mode)
    if type(series_id) is not int or series_id <= 0:
        raise ValueError("Выберите числовой ID ряда")
    row = db.execute(f"""SELECT s.*,x.mode,x.stream,x.code AS source {JOIN} WHERE x.mode=? AND s.id=?""",
                     (mode, series_id)).fetchone()
    if not row:
        raise ValueError("Ряд не найден в выбранном режиме")
    return dict(row)


def rows_for(db, series_id, start, end, as_of):
    return [dict(row) for row in db.execute("""
        WITH versions AS (
            SELECT id,start,end,event_date,retrieved,recorded,version,number,quality,reference,payload,
                   row_number() OVER (PARTITION BY start,end ORDER BY version DESC) AS revision_order
            FROM memory_observations WHERE series_id=? AND start>=? AND end<=?
            AND (? IS NULL OR recorded<=?)
        )
        SELECT * FROM versions WHERE revision_order=1 ORDER BY start,end LIMIT 2200
    """, (series_id, start, end, as_of, as_of))]


def summarize(rows, period, aggregation):
    start, end, length = period
    info = {"period": [start, end], "days": length, "value": None, "observations": []}
    exact = [row for row in rows if (row["start"], row["end"]) == (start, end)]
    if exact:
        rows = exact
    if not rows:
        return info | {"status": "missing"}
    info["observations"] = [row["id"] for row in rows[:32]]
    required = date.fromisoformat(start)
    covered, weighted = 0, []
    for row in rows:
        day_start, day_end = map(date.fromisoformat, (row["start"], row["end"]))
        if day_start != required:
            return info | {"status": "missing" if day_start > required else "overlapping_windows"}
        if row["quality"] != "complete" or row["number"] is None:
            return info | {"status": "partial"}
        width = (day_end - day_start).days + 1
        covered += width
        weighted.append((row["number"], width))
        required = day_end + timedelta(days=1)
    if covered != length:
        return info | {"status": "missing"}
    if len(weighted) == 1:
        value = weighted[0][0]
    elif aggregation == "sum":
        value = sum(number for number, _ in weighted)
    elif aggregation == "mean" and len({days for _, days in weighted}) == 1:
        value = sum(number for number, _ in weighted) / len(weighted)
    else:
        return info | {"status": "requires_exact_observation"}
    return info | {"status": "ok", "value": value}


def compare(request):
    mode = require_mode(request.get("mode", "local"))
    before, after = window(request["before"]), window(request["after"])
    as_of = memory.stamp(request["as_of"]) if request.get("as_of") else None
    with ai_store.database() as db:
        series = series_for(db, mode, request["series_id"])
        earlier = summarize(rows_for(db, series["id"], *before[:2], as_of), before, series["aggregation"])
        later = summarize(rows_for(db, series["id"], *after[:2], as_of), after, series["aggregation"])
        denominator_id = request.get("denominator_series_id")
        if denominator_id is not None:
            base = series_for(db, mode, denominator_id)
            fields = ("product", "source_id", "segment", "timezone", "dimensions")
            if (series["id"] == base["id"] or any(series[key] != base[key] for key in fields)
                    or series["aggregation"] != "sum" or base["aggregation"] != "sum"):
                raise ValueError("Для конверсии нужны числитель и база одного продукта, источника и сегмента")
            for observed, period in ((earlier, before), (later, after)):
                denominator = summarize(rows_for(db, base["id"], *period[:2], as_of), period, base["aggregation"])
                divisor = denominator["value"]
                if observed["status"] != "ok" or denominator["status"] != "ok" or not divisor:
                    observed.update(status="missing_or_zero_denominator", value=None)
                else:
                    observed.update(numerator=observed["value"], denominator=divisor,
                                    value=100 * observed["value"] / divisor)
                observed["observations"] = (observed["observations"] + denominator["observations"])[:32]
            series = series | {"unit": "percent", "denominator_series_id": denominator_id}
    warnings = []
    if mode != "synthetic":
        try:
            zone = ZoneInfo(series["timezone"])
        except ZoneInfoNotFoundError:
            later.update(status="unknown_timezone", value=None)
            warnings.append("Часовой пояс источника недоступен.")
        else:
            clock = datetime.fromisoformat(as_of) if as_of else datetime.now(zone)
            today = clock.astimezone(zone).date().isoformat()
            if after[1] >= today:
                later.update(status="partial_day", value=None)
                warnings.append("Последнее окно включает незавершённый или будущий день.")
    if before[1] >= after[0]:
        warnings.append("Окна должны следовать друг за другом без пересечения.")
    if before[2] != after[2]:
        warnings.append("Окна различаются по длительности.")
    if before[2] % 7 and date.fromisoformat(before[0]).weekday() != date.fromisoformat(after[0]).weekday():
        warnings.append("Разный состав рабочих и выходных дней. Поправка на сезонность не выполнена.")
    ready = earlier["status"] == later["status"] == "ok" and before[1] < after[0] and before[2] == after[2]
    first, last = earlier["value"], later["value"]
    return {"series": series, "before": earlier, "after": later, "warnings": warnings,
            "status": "ok" if ready else "incomparable",
            "delta": last - first if ready else None,
            "delta_percent": (last / first - 1) * 100 if ready and first else None}


def timeline(request):
    mode = require_mode(request.get("mode", "local"))
    start, end, _ = window(request["period"])
    as_of = memory.stamp(request["as_of"]) if request.get("as_of") else None
    with ai_store.database() as db:
        series = series_for(db, mode, request["series_id"])
        rows = rows_for(db, series["id"], start, end, as_of)
    return {"series": series, "observations": [
        {key: value for key, value in row.items() if key != "revision_order"} |
        {"payload": json.loads(row["payload"])} for row in rows[:250]
    ], "truncated": len(rows) > 250}


def competitor_changes(request):
    mode = require_mode(request.get("mode", "local"))
    start, end, _ = window(request["period"])
    product = request.get("product")
    output = []
    with ai_store.database() as db:
        for row in db.execute(f"""SELECT s.id,s.product,s.subject,s.unit,s.dimensions {JOIN}
                 WHERE x.mode=? AND x.stream='competitor' AND s.metric='price'
                 AND (? IS NULL OR s.product=?) LIMIT 80""", (mode, product, product)):
            records = rows_for(db, row["id"], start, end, None)
            previous = None
            for item in records:
                if item["quality"] not in {"complete", "excerpt"} or item["number"] is None:
                    previous = None
                    continue
                if previous and previous["number"] != item["number"]:
                    output.append({"series_id": row["id"], "product": row["product"], "competitor": row["subject"],
                                   "currency": row["unit"], "terms": json.loads(row["dimensions"]).get("terms"),
                                   "before": previous["number"], "after": item["number"],
                                   "observed": item["start"], "published": item["event_date"],
                                   "reference": item["reference"], "observation_id": item["id"],
                                   "quality": item["quality"]})
                previous = item
    return sorted(output, key=lambda item: item["observed"], reverse=True)[:50]


def primary_signatures(evidence):
    return {item["id"]: memory.digest([
        item.get("kind"), item.get("ref"), item.get("text"), item.get("period"), item.get("values"),
        item.get("quality"), item.get("source_group"),
    ]) for item in evidence if item.get("kind") not in {"memory_context", "historical_comparison"}
        and item.get("quality") not in {"reported", "derived", "incomplete", "snippet"}}


def past_decisions(mode, query, *, start=None, end=None, limit=12, internal=False):
    require_mode(mode)
    words = query.casefold().split()[:8]
    with ai_store.database() as db:
        rows = db.execute("""
            SELECT r.id,r.created,r.report,t.action,t.body,
                (SELECT body FROM reviews WHERE task_id=t.id ORDER BY created DESC LIMIT 1) AS review
            FROM runs r LEFT JOIN tasks t ON t.run_id=r.id
            WHERE json_extract(r.report,'$.data_mode')=?
            AND (? IS NULL OR substr(r.created,1,10)>=?) AND (? IS NULL OR substr(r.created,1,10)<=?)
            ORDER BY r.created DESC LIMIT 300
        """, (mode, start, start, end, end))
        matches = []
        for row in rows:
            report = json.loads(row["report"])
            task = json.loads(row["body"]) if row["body"] else {}
            context = report.get("request_context", {}).get("query", "")
            text = " ".join([context, report.get("summary", ""), task.get("title", ""), task.get("hypothesis", "")])
            if words and not any(word in text.casefold() for word in words):
                continue
            review = json.loads(row["review"]) if row["review"] else {}
            matches.append({"run_id": row["id"], "created": row["created"], "question": context,
                            "summary": report.get("summary"), "proposal": task.get("title"),
                            "decision": row["action"], "decision_reason": task.get("decision_reason"),
                            "effect": review.get("status"), "classification": "historical_context"})
            if internal:
                matches[-1]["hypothesis"] = task.get("hypothesis", "")
                matches[-1]["primary_signatures"] = list(primary_signatures(report.get("evidence", [])).values())
            if len(matches) == limit:
                break
        return matches


def overview(mode):
    require_mode(mode)
    with ai_store.database() as db:
        counts = db.execute(f"""SELECT count(o.id) AS observations,count(DISTINCT s.source_id) AS sources,
                   min(o.start) AS first_day,max(o.end) AS last_day {JOIN}
                   LEFT JOIN memory_observations o ON o.series_id=s.id WHERE x.mode=?""", (mode,)).fetchone()
        jobs = db.execute("""SELECT kind,status,started,finished,detail FROM memory_sync_jobs
                             ORDER BY started DESC LIMIT 15""").fetchall()
        documents = db.execute("SELECT count(*) FROM memory_documents").fetchone()[0] if mode == "local" else 0
    return dict(counts) | {"documents": documents, "jobs": [dict(row) for row in jobs]}


def search(request):
    query = memory.label(request.get("query", ""), 500)
    mode = require_mode(request.get("mode", "local"))
    start = memory.iso(request["start"]) if request.get("start") else None
    end = memory.iso(request["end"]) if request.get("end") else None
    product = memory.label(request["product"]) if request.get("product") else None
    with ai_store.database() as db:
        documents = (memory.find_fragments(db, query, product=product, kind=request.get("kind"),
                                           start=start, end=end) if mode == "local" else [])
    return {"documents": documents, "decisions": past_decisions(mode, query, start=start, end=end)}


def backup_status():
    import os
    from pathlib import Path

    name = os.getenv("PM_BACKUP_STATUS_FILE")
    if not name:
        return {"status": "unknown"}
    path = Path(name)
    try:
        if path.stat().st_size > 4000:
            return {"status": "unknown"}
        supplied = json.loads(path.read_text(encoding="utf8"))
        status = supplied.get("status")
        answer = {"status": status if status in {"ok", "error"} else "unknown"}
        for name in ("last_backup", "last_integrity_check", "last_restore_check"):
            if supplied.get(name):
                answer[name] = memory.stamp(supplied[name])
        return answer
    except (OSError, TypeError, ValueError, AttributeError):
        return {"status": "unknown"}


def product_history(request):
    mode = require_mode(request.get("mode", "local"))
    name = memory.label(request["product"])
    start, end, _ = window(request["period"])
    as_of = memory.stamp(request["as_of"]) if request.get("as_of") else None
    series_id = request.get("series_id")
    with ai_store.database() as db:
        targets = db.execute(f"""SELECT s.id,s.subject,s.method,s.unit,x.stream {JOIN}
                              WHERE x.mode=? AND s.product=? AND x.stream='release' LIMIT 150""",
                             (mode, name)).fetchall()
        series = series_for(db, mode, series_id) if series_id is not None else None
        if series and (series["product"] != name or series["stream"] != "sales"):
            raise ValueError("Для эффекта релиза укажите ряд продаж этого продукта")
        result = []
        for target in targets:
            for row in rows_for(db, target["id"], start, end, as_of):
                if row["quality"] == "retracted":
                    continue
                observed = row["event_date"] or row["start"]
                facts = {"id": target["subject"], "date": observed, "observation_id": row["id"],
                         "metadata": json.loads(row["payload"])}
                if series:
                    release = date.fromisoformat(observed)
                    launch_week = release - timedelta(days=release.weekday())
                    before = ((launch_week - timedelta(days=28)).isoformat(),
                              (launch_week - timedelta(days=1)).isoformat(), 28)
                    after = ((launch_week + timedelta(days=7)).isoformat(),
                             (launch_week + timedelta(days=34)).isoformat(), 28)
                    first = summarize(rows_for(db, series_id, *before[:2], as_of), before, series["aggregation"])
                    last = summarize(rows_for(db, series_id, *after[:2], as_of), after, series["aggregation"])
                    facts["comparison"] = {
                        "before": first, "after": last,
                        "delta": last["value"] - first["value"] if first["status"] == last["status"] == "ok" else None,
                        "interpretation": "descriptive_only",
                    }
                result.append(facts)
    return {"product": name, "releases": sorted(result, key=lambda item: item["date"], reverse=True)[:25]}


def save_note(request):
    text = memory.label(request.get("text"), 1800)
    if reidentification(text):
        raise ValueError("Удалите персональные контакты из заметки")
    product = memory.label(request["product"]) if request.get("product") else None
    day = memory.iso(request["observed"]) if request.get("observed") else None
    category = "Конкуренты" if "конкурент" in text.casefold() else "Пользовательская заметка"
    identifier = memory.digest([text, product, day])
    ai_store.store_excerpts("note:" + identifier, [
        {"id": identifier, "kind": category, "ref": "Заметка оператора", "text": text,
         "product": product, "observed_at": day, "locator": {"origin": "user_reported"}}
    ])
    return {"status": "reported", "category": category, "id": identifier}


def reidentification(text):
    import re
    return re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+[.][A-Za-z]{2,}|\+\d[\d ()-]{9,}", text)
