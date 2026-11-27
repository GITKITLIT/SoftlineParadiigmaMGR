"""Private history, deterministic change detection and provider cost estimates."""

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from .market_matrix import record_day
from .parsing import utc_stamp
from .storage import ensure_private_database

_TARIFF_DAY = Decimal("0.488")
_TARIFF_NIGHT = Decimal("0.366")
_MOSCOW = timezone(timedelta(hours=3))
_SIGNIFICANT_DISCRETE = {
    "version", "license_type", "duration", "devices", "subscription", "availability",
    "self_checkout", "purchase_flow", "payment", "support", "consultation", "account",
    "renewal", "services", "assortment", "features",
}


def estimate_cost(result):
    """Compute a nominal upper estimate from attempted requests, independent of billing."""
    requests = result["usage"]["requests"]
    total = Decimal(0)
    accounted = 0
    for attempt in result.get("attempts", ()):
        started = attempt.get("started_at")
        if not started:
            continue
        try:
            timestamp = datetime.fromisoformat(started.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        if timestamp.utcoffset() is not None and accounted < requests:
            local = timestamp.astimezone(_MOSCOW)
            elapsed_ms = attempt.get("elapsed_ms", 0)
            if type(elapsed_ms) not in {int, float} or not 0 <= elapsed_ms < 86400000:
                elapsed_ms = 86400000
            end = local + timedelta(milliseconds=elapsed_ms)
            night = local.date() == end.date() and max(local.hour, end.hour) < 8
            total += _TARIFF_NIGHT if night else _TARIFF_DAY
            accounted += 1
    total += _TARIFF_DAY * (requests - accounted)
    return {
        "currency": "RUB", "estimated_upper_rub": f"{total:.3f}",
        "requests": requests, "requests_with_timestamps": accounted,
        "tariff_checked_at": "2026-11-27", "includes_vat": True,
    }


def history_scope(plan, mode):
    raw = json.dumps([
        mode, plan["product"], sorted(set(plan["domains"])), sorted(set(plan["topics"])),
    ], ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _rows(snapshot):
    return {
        (feature["feature"], name.casefold()): (name, cell)
        for feature in snapshot["matrix"]["rows"]
        for name, cell in feature["competitors"].items()
    }


def _price_change(before, after, threshold):
    conditions = before.get("conditions")
    required = ("product", "version", "license_type", "duration", "devices", "tax", "currency")
    if not conditions or conditions != after.get("conditions") or not all(
        conditions.get(field) for field in required
    ):
        return "different_offers", False, None
    previous, current = Decimal(before["key"]), Decimal(after["key"])
    pct = (current - previous) / previous * 100 if previous else None
    material = abs(pct) >= threshold if pct is not None else current != previous
    delta = {
        "currency": conditions["currency"].upper(), "amount": format(current - previous, "f"),
        "percent": format(pct.quantize(Decimal("0.01")), "f") if pct is not None else None,
    }
    return "changed", material, delta


def compare_snapshots(before, after, *, price_threshold_pct=5):
    """Compare confirmed observations with compatible scope and an advancing source date."""
    threshold = Decimal(str(price_threshold_pct))
    if not threshold.is_finite() or not 0 < threshold <= 100:
        raise ValueError("invalid_materiality_threshold")
    if before is None:
        return {"baseline": None, "changes": [], "material_changes": 0}
    if before["scope"] != after["scope"]:
        raise ValueError("incompatible_snapshot_scope")
    earlier, later = _rows(before), _rows(after)
    changes = []
    for feature, key in sorted(earlier.keys() | later.keys()):
        name, _ = later.get((feature, key), earlier.get((feature, key)))
        previous = earlier.get((feature, key), ("", {}))[1]
        current = later.get((feature, key), ("", {}))[1]
        if previous.get("key") == current.get("key") and previous.get("status") == current.get("status"):
            if previous.get("conditions") == current.get("conditions"):
                continue
        before_status, after_status = previous.get("status"), current.get("status")
        if {before_status, after_status} <= {None, "unknown"}:
            continue
        material, delta = False, None
        before_day, after_day = record_day(previous.get("as_of")), record_day(current.get("as_of"))
        if before_status != "confirmed" or after_status != "confirmed":
            reason = "new_evidence" if before_status in {None, "unknown"} and after_status == "confirmed" else "needs_review"
        elif not (before_day and after_day and after_day > before_day):
            reason = "same_or_older_observation"
        elif previous.get("subject") != current.get("subject") or (
            feature not in {"version", "price"} and previous.get("version") != current.get("version")
        ):
            reason = "different_offers"
        elif feature == "price":
            reason, material, delta = _price_change(previous, current, threshold)
        else:
            reason = "changed"
            material = feature in _SIGNIFICANT_DISCRETE
        changes.append({
            "feature": feature, "competitor": name, "reason": reason, "material": material,
            "before": {field: previous.get(field) for field in ("value", "status", "as_of", "evidence_ids")},
            "after": {field: current.get(field) for field in ("value", "status", "as_of", "evidence_ids")},
            "price_delta": delta,
            "before_sources": [before["evidence_index"][ref] for ref in previous.get("evidence_ids", ())
                               if ref in before["evidence_index"]],
            "after_sources": [after["evidence_index"][ref] for ref in current.get("evidence_ids", ())
                              if ref in after["evidence_index"]],
        })
    return {
        "baseline": before["id"], "changes": changes,
        "material_changes": sum(change["material"] for change in changes),
    }


class History:
    def __init__(self, directory: Path):
        directory = directory.resolve(strict=True)
        if not directory.is_dir() or any(
            (folder / ".git").exists() for folder in (directory, *directory.parents)
        ):
            raise ValueError("history_requires_private_directory")
        path = directory / "competitor-history.sqlite3"
        if path.is_symlink():
            raise ValueError("invalid_history_path")
        self.path = path
        ensure_private_database(path)
        with closing(self._db()) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS run_slot (
                    scope TEXT PRIMARY KEY, token TEXT, started REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS snapshot (
                    id TEXT PRIMARY KEY, scope TEXT NOT NULL, captured REAL NOT NULL,
                    usable INTEGER NOT NULL, data TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS snapshot_scope_time ON snapshot(scope, captured DESC);
            """)

    def _db(self):
        return sqlite3.connect(self.path, timeout=3)

    def claim(self, scope, now, interval_seconds):
        with closing(self._db()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT token, started FROM run_slot WHERE scope=?", (scope,)).fetchone()
            when = now.timestamp()
            if row:
                current, last = row
                if current is not None and when < last + 1800:
                    return None, "already_running"
                if when < last + interval_seconds:
                    return None, "not_due"
            token = uuid4().hex
            db.execute("""
                INSERT INTO run_slot(scope, token, started) VALUES (?, ?, ?)
                ON CONFLICT(scope) DO UPDATE SET token=excluded.token, started=excluded.started
            """, (scope, token, when))
            return token, None

    def finish(self, scope, token, snapshot=None, *, usable=False):
        with closing(self._db()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute(
                "UPDATE run_slot SET token=NULL WHERE scope=? AND token=?", (scope, token),
            ).rowcount
            if not changed:
                raise ValueError("history_claim_expired")
            if snapshot is not None:
                captured = datetime.fromisoformat(snapshot["captured_at"].replace("Z", "+00:00"))
                db.execute("INSERT INTO snapshot VALUES (?, ?, ?, ?, ?)", (
                    snapshot["id"], scope, captured.timestamp(), int(usable),
                    json.dumps(snapshot, ensure_ascii=False, allow_nan=False),
                ))

    def latest(self, scope, *, usable=False):
        with closing(self._db()) as db:
            row = db.execute("""
                SELECT data FROM snapshot WHERE scope=? AND (?=0 OR usable=1)
                ORDER BY captured DESC, rowid DESC LIMIT 1
            """, (scope, int(usable))).fetchone()
            return json.loads(row[0]) if row else None


def make_snapshot(context, result, *, scope, now, mode):
    return {
        "schema_version": 1, "id": uuid4().hex, "scope": scope, "mode": mode,
        "captured_at": utc_stamp(now), "matrix": context["matrix"],
        "evidence_index": context["evidence_index"], "qwen_context": context["qwen_context"],
        "search": {
            "status": result["status"], "reason": result["reason"], "warnings": result["warnings"],
            "usage": result["usage"], "page_usage": result.get("page_usage"),
            "metrics": result.get("metrics"), "coverage": result.get("coverage", []),
            "attempts": result.get("attempts", []),
        },
        "cost": estimate_cost(result) if mode == "live" else None,
    }
