"""Project private snapshots into the published Product Intelligence Memory schema."""

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from .snapshots import _rows, compare_snapshots

SOURCE = "competitive_snapshot_v1"
_TERMS = ("product", "version", "license_type", "duration", "devices", "tax", "currency")


def _identifier(snapshot_id, feature, competitor):
    key = json.dumps([snapshot_id, feature, competitor], ensure_ascii=False)
    return "competitor-observation:" + hashlib.sha256(key.encode()).hexdigest()


def _timestamp(value):
    if isinstance(value, str) and "T" in value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.utcoffset() is not None:
                return parsed.astimezone(UTC).isoformat()
        except (ValueError, OverflowError):
            pass
    return None


def history_batch(directory: Path, *, after=0, limit=20):
    """Read snapshots and their preceding usable snapshot without opening a write connection."""
    if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError("invalid_history_cursor")
    path = Path(directory) / "competitor-history.sqlite3"
    if path.is_symlink():
        raise ValueError("invalid_history_path")
    uri = path.resolve(strict=True).as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=3)) as db:
        rows = db.execute("""
            SELECT cur.rowid, cur.data, (
                SELECT prior.data FROM snapshot AS prior
                WHERE prior.scope = cur.scope AND prior.usable = 1 AND prior.rowid < cur.rowid
                ORDER BY prior.captured DESC, prior.rowid DESC LIMIT 1
            )
            FROM snapshot AS cur WHERE cur.rowid > ? ORDER BY cur.rowid LIMIT ?
        """, (after, limit)).fetchall()
    return [
        {"cursor": seq, "snapshot": json.loads(data), "previous": json.loads(prior) if prior else None}
        for seq, data, prior in rows
    ]


def _references(snapshot, identifiers):
    index = snapshot["evidence_index"]
    return [dict(index[identifier]) for identifier in dict.fromkeys(identifiers) if identifier in index]


def project_snapshot(snapshot, previous=None, *, mode):
    """Build native PIM inputs. The operator explicitly selects the local or synthetic partition."""
    if mode not in {"local", "synthetic"}:
        raise ValueError("invalid_pim_mode")
    if snapshot.get("mode") == "demo" and mode != "synthetic":
        raise ValueError("synthetic_snapshot_cannot_enter_local_memory")
    if snapshot.get("schema_version") != 1:
        raise ValueError("unsupported_snapshot")
    if previous is not None and previous["scope"] != snapshot["scope"]:
        raise ValueError("incompatible_snapshot_scope")
    stored = snapshot.get("changes_from_previous")
    previous_id = previous["id"] if previous else None
    if stored is not None and stored["baseline"] != previous_id:
        raise ValueError("wrong_snapshot_predecessor")
    changes = stored if stored is not None else compare_snapshots(previous, snapshot)
    by_feature = {(item["feature"], item["competitor"].casefold()): item for item in changes["changes"]}
    previous_rows = _rows(previous) if previous else {}
    captured = _timestamp(snapshot["captured_at"])
    if captured is None:
        raise ValueError("invalid_capture_timestamp")
    result = []
    for (feature, competitor_key), (competitor, cell) in sorted(_rows(snapshot).items()):
        key = (feature, competitor_key)
        product = snapshot.get("plan", {}).get("product") or cell.get("subject")
        if not product:
            raise ValueError("snapshot_requires_product")
        observed = cell.get("as_of")
        refs = _references(snapshot, cell.get("evidence_ids", []))
        primary_ids = cell.get("fact_evidence_ids") or cell.get("evidence_ids", [])
        primary = next(iter(_references(snapshot, primary_ids)), {})
        received = _timestamp(primary.get("retrieved_at") or primary.get("as_of"))
        status, scope = cell["status"], cell.get("scope")
        conditions = cell.get("conditions") or {}
        confirmed_price = feature == "price" and status == "confirmed" and all(
            conditions.get(field) for field in _TERMS
        )
        source_type = {
            "public_page": "page_excerpt", "search_snippet": "snippet",
            "local_study": "local_study",
        }.get(scope)
        quality = "missing" if status == "unknown" else "reported"
        if scope == "search_snippet":
            quality = "snippet"
        elif status == "confirmed":
            quality = "excerpt" if scope == "public_page" else "complete"
        metadata = {
            "observation_id": _identifier(snapshot["id"], *key),
            "snapshot_id": snapshot["id"], "previous_snapshot_id": previous_id,
            "previous_observation_id": _identifier(previous_id, *key) if key in previous_rows else None,
            "competitor": competitor, "product": product, "product_version": cell.get("version"),
            "feature": feature, "observed_value": cell.get("value"),
            "price": cell.get("key") if feature == "price" else None,
            "currency": conditions["currency"].upper() if conditions.get("currency") else None,
            "license_terms": {field: conditions.get(field) for field in (
                "license_type", "duration", "devices", "tax", "subscription"
            )} if conditions else None,
            "purchase_conditions": cell.get("value") if feature in {
                "self_checkout", "purchase_flow", "payment", "consultation", "renewal",
            } else None,
            "observed_at": observed, "retrieved_at": received, "captured_at": captured,
            "source_url": primary.get("url") or (primary.get("ref") if source_type in {
                "snippet", "page_excerpt",
            } else None),
            "source_type": source_type, "reliability": status, "sources": refs,
            "alternatives": [
                {field: alternative.get(field) for field in ("value", "status", "as_of", "fact_evidence_ids")}
                for alternative in cell.get("alternatives", [])
            ],
            "change": by_feature.get(key),
        }
        # References are already in the evidence index. Avoid storing both expanded versions twice.
        if metadata["change"]:
            metadata["change"] = {field: value for field, value in metadata["change"].items()
                                  if field not in {"before_sources", "after_sources"}}
        # PIM bounds its metadata to 12,000 characters. Keep the IDs for all citations.
        if len(json.dumps(metadata, ensure_ascii=False, allow_nan=False)) > 11000:
            metadata["source_ids"] = [reference["id"] for reference in refs]
            metadata["sources"] = [{field: primary.get(field) for field in (
                "id", "ref", "url", "scope", "as_of", "retrieved_at", "provenance",
            )}] if primary else []
            metadata["source_details_truncated"] = True
        dims = {"scope": snapshot["scope"]}
        if conditions and feature == "price":
            # PIM uses terms to keep differing versions and license conditions in different series.
            term_values = [conditions.get(field) for field in (*_TERMS, "subscription")]
            dims["terms_key"] = hashlib.sha256(json.dumps(term_values, ensure_ascii=False).encode()).hexdigest()
            description = " | ".join(value for index, value in enumerate(term_values)
                                     if value and index != _TERMS.index("currency"))
            if len(description) <= 350:
                dims["terms"] = description
        result.append({
            "mode": mode, "stream": "competitor", "source": SOURCE,
            "product": product, "subject": competitor, "metric": feature,
            "unit": conditions["currency"].upper() if feature == "price" and conditions.get("currency") else "text",
            "method": "competitive-snapshot-v1", "aggregation": "snapshot",
            "quality": quality, "value": float(cell["key"]) if confirmed_price else None,
            "start": observed or captured[:10], "event_date": observed,
            "retrieved": received or captured, "reference": primary.get("ref"),
            "metadata": metadata, "dimensions": dims,
        })
    return result


def write_pim_snapshot(snapshot, previous=None, *, mode):
    """Insert one snapshot atomically through AI Core and replay by stable observation ID."""
    from pm import ai_store, memory

    rows = project_snapshot(snapshot, previous, mode=mode)
    if not rows:
        return {"created": 0, "ids": []}
    if len(rows) > 240:
        raise ValueError("too_many_snapshot_observations")
    identifiers = [row["metadata"]["observation_id"] for row in rows]
    placeholders = ",".join("?" for _ in identifiers)
    with ai_store.database() as db:
        saved = dict(db.execute(f"""
            SELECT json_extract(o.payload, '$.observation_id'), o.id FROM memory_observations o
            JOIN memory_series s ON o.series_id = s.id
            JOIN memory_sources x ON s.source_id = x.id
            WHERE x.mode = ? AND x.stream = 'competitor' AND x.code = ?
              AND o.start BETWEEN ? AND ? AND json_extract(o.payload, '$.observation_id') IN ({placeholders})
        """, (mode, SOURCE, min(row["start"] for row in rows), max(row["start"] for row in rows),
              *identifiers)).fetchall())
        ids, created = [], 0
        for identifier, row in zip(identifiers, rows, strict=True):
            if identifier in saved:
                ids.append(saved[identifier])
            else:
                pim_id, inserted = memory.observation(db, row)
                ids.append(pim_id)
                created += inserted
    return {"created": created, "ids": ids}
