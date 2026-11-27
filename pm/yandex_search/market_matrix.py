"""Evidence selection, comparison table and bounded handoff to AI Core."""

import json
import re
from collections import defaultdict
from datetime import UTC, date, datetime
from decimal import Decimal

FEATURES = {
    "price": "Цена", "version": "Версия", "license_type": "Тип лицензии",
    "duration": "Срок лицензии", "devices": "Устройства", "tax": "НДС", "currency": "Валюта",
    "subscription": "Подписка", "assortment": "Ассортимент", "features": "Характеристики",
    "license_selection": "Подбор продукта", "self_checkout": "Самостоятельная покупка",
    "purchase_flow": "Оформление заказа", "payment": "Способы оплаты",
    "consultation": "Консультации", "support": "Поддержка", "account": "Личный кабинет",
    "renewal": "Продление лицензий", "services": "Дополнительные сервисы",
    "availability": "Наличие",
}
_UX = {"license_selection", "self_checkout", "purchase_flow", "payment", "consultation",
       "support", "account", "renewal"}
_BOOLEAN_FEATURES = _UX | {"availability"}
_YES = {"да", "есть", "доступна", "доступен", "доступно", "доступны", "yes", "available", "instock"}
_NO = {"нет", "недоступна", "недоступен", "недоступно", "no", "unavailable", "outofstock", "soldout"}
_UNKNOWN = {"", "-", "—", "неизвестно", "н/д", "n/a", "unknown", "нет данных", "не найдено"}
_PRODUCT_SPECIFIC = {"price", "version", "license_type", "duration", "subscription", "devices", "features"}
_QUALITY = {"internal_offer": 4, "public_page": 3, "local_study": 2, "search_snippet": 1}
_STATES = {"confirmed": 4, "undated": 3, "unverified": 2, "stale": 1}


def _short(value):
    if type(value) in (int, float):
        value = str(value)
    if not isinstance(value, str):
        return None
    return " ".join(value.split())[:240] or None


def record_day(value):
    if type(value) is date:
        return value
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        pass
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return timestamp.astimezone(UTC).date() if timestamp.utcoffset() is not None else None
    except (ValueError, OverflowError):
        return None


def _value(raw, feature):
    text = _short(raw)
    key = text.casefold() if text else ""
    if key in _UNKNOWN:
        return None, None
    if feature in _BOOLEAN_FEATURES:
        if key in _YES:
            return text, "yes"
        if key in _NO:
            return text, "no"
    if feature == "price":
        normalized = key.replace(" ", "").replace(",", ".")
        if re.fullmatch(r"[0-9]{1,12}(?:\.[0-9]{1,4})?", normalized):
            return text, format(Decimal(normalized).normalize(), "f")
        return None, None
    return text, key


def _day(source, data):
    if source["scope"] in {"public_page", "search_snippet"}:
        return record_day(source.get("retrieved_at"))
    return record_day(data.get("study_date"))


def _evidence_id(source):
    return source["id"]


def _sources_with_terms(records):
    groups = defaultdict(list)
    for source, data in records:
        if source["scope"] == "local_study" and data.get("offer_group"):
            groups[data["offer_group"]].append((source, data))
        else:
            conflicts = {key: [(source, value) for value in values]
                         for key, values in data.get("attribute_conflicts", {}).items()}
            yield source, data, {}, conflicts
    for members in groups.values():
        source = members[0][0]
        values, refs, conflicts = {}, defaultdict(list), defaultdict(list)
        for origin, data in members:
            for field, raw in data.items():
                if field not in FEATURES and field not in {
                    "competitor", "product", "study_date", "version", "devices", "currency", "tax",
                }:
                    continue
                if not _short(raw):
                    continue
                if _short(values.setdefault(field, raw)) != _short(raw):
                    conflicts[field].append((origin, raw))
                refs[field].append(origin)
        for field, alternatives in conflicts.items():
            alternatives[:0] = [(refs[field][0], values.pop(field))]
        yield source, values, refs, conflicts


def _facts(records, as_of, offer, product):
    facts = defaultdict(list)
    for source, data, refs, within_source_conflicts in _sources_with_terms(records):
        scope = source["scope"]
        as_at = _day(source, data)
        terms = offer(data)
        if product and terms["product"] and terms["product"] != product.casefold():
            continue
        expiry = record_day(data.get("price_valid_until"))
        for feature in FEATURES:
            status = "confirmed"
            horizon = 30 if feature in {"price", "availability"} else 120
            if (as_at and (as_of - as_at).days > horizon) or (feature == "price" and expiry and expiry < as_of):
                status = "stale"
            elif scope == "search_snippet":
                status = "unverified"
            elif as_at is None or as_at > as_of:
                status = "undated"

            primary = refs.get(feature, (source,))
            candidates = [(primary, data.get(feature))]
            candidates.extend(([origin], value) for origin, value in within_source_conflicts.get(feature, ()))
            for origins, raw in candidates:
                value, key = _value(raw, feature)
                if key is None:
                    continue
                provenance = {_evidence_id(origin): origin for origin in origins[:2]}
                conditions, condition_ids = None, []
                if feature == "price":
                    conditions = {field: text or None for field, text in terms.items() if field != "price"}
                    for name, condition in conditions.items():
                        if condition:
                            for origin in refs.get(name, (source,))[:1]:
                                condition_id = _evidence_id(origin)
                                provenance[condition_id] = origin
                                if condition_id not in condition_ids:
                                    condition_ids.append(condition_id)
                facts[feature].append({
                    "value": value, "key": key, "status": status, "scope": scope,
                    "as_of": as_at.isoformat() if as_at else None,
                    "subject": terms["product"] or None, "version": terms["version"] or None,
                    "source_quality": _QUALITY[scope], "evidence_ids": list(provenance),
                    "fact_evidence_ids": [_evidence_id(origin) for origin in origins[:2]],
                    "conditions": conditions, "condition_evidence_ids": condition_ids,
                })
    return facts


def _rank(fact):
    return _STATES[fact["status"]], fact["source_quality"], fact["as_of"] or ""


def _conditions_key(fact):
    return tuple((fact["conditions"] or {}).values())


def _cell(facts):
    ordered = sorted(facts, key=_rank, reverse=True)
    if not ordered:
        return {
            "value": None, "status": "unknown", "as_of": None,
            "evidence_ids": [], "alternatives": [],
        }
    best = ordered[0]
    same, alternatives = [], []
    seen = {(best["key"], _conditions_key(best), best["status"])}
    for candidate in ordered[1:]:
        identity = candidate["key"], _conditions_key(candidate), candidate["status"]
        if identity in seen:
            if identity == (best["key"], _conditions_key(best), best["status"]) and len(same) < 2:
                same.append(candidate)
            continue
        seen.add(identity)
        if candidate["key"] != best["key"] or _conditions_key(candidate) != _conditions_key(best):
            alternatives.append(candidate)
    conflict = any(
        best["key"] != row["key"] and _conditions_key(best) == _conditions_key(row)
        and best["status"] == row["status"] == "confirmed"
        for row in alternatives
    )
    different_terms = best["conditions"] and any(
        best["status"] == row["status"] == "confirmed"
        and _conditions_key(best) != _conditions_key(row) for row in alternatives
    )
    alternatives = alternatives[:3]
    public = {key: value for key, value in best.items() if key != "source_quality"}
    public["status"] = "conflict" if conflict else "incomparable" if different_terms else best["status"]
    public["alternatives"] = [
        {key: value for key, value in option.items() if key not in {"source_quality", "key"}}
        for option in alternatives
    ]
    public["evidence_ids"] = list(dict.fromkeys(
        best["evidence_ids"]
        + [i for option in alternatives + same for i in option["evidence_ids"]]
    ))
    return public


def _relation(feature, own, competitor):
    states = {own["status"], competitor["status"]}
    for status in ("conflict", "incomparable", "unknown", "stale", "unverified", "undated"):
        if status in states:
            return {"status": status}
    if feature == "price":
        left, right = own["conditions"], competitor["conditions"]
        required = {"product", "version", "license_type", "duration", "devices", "tax", "currency"}
        missing = {side: sorted(field for field in required if not terms.get(field))
                   for side, terms in (("own", left), ("competitor", right))}
        mismatches = sorted(field for field in left if left[field] != right.get(field))
        if any(missing.values()) or mismatches or own["as_of"] != competitor["as_of"]:
            return {
                "status": "incomparable", "unknown_terms": missing, "different_terms": mismatches,
                "same_date": own["as_of"] == competitor["as_of"],
            }
        diff = Decimal(competitor["key"]) - Decimal(own["key"])
        return {
            "status": "advantage" if diff > 0 else "disadvantage" if diff < 0 else "match",
            "competitor_minus_own": format(diff, "f"), "currency": left["currency"].upper(),
        }
    if feature in _PRODUCT_SPECIFIC and (
        not own["subject"] or own["subject"] != competitor["subject"]
        or (feature != "version" and own["version"] != competitor["version"])
    ):
        return {"status": "incomparable", "reason": "product_or_version"}
    if own["key"] == competitor["key"]:
        return {"status": "match"}
    if {own["key"], competitor["key"]} == {"yes", "no"}:
        return {"status": "advantage" if own["key"] == "yes" else "disadvantage"}
    return {"status": "different"}


def _compact_evidence(source):
    return {
        "id": source["id"], "ref": source["ref"], "scope": source["scope"],
        "as_of": source.get("study_date") or source.get("retrieved_at"),
        "excerpt": (_short(source.get("text")) or "")[:180] or None,
    }


def build_market_matrix(
    *, sources, own_source, offer, as_of, aliases, max_competitors, max_sources, product,
):
    lookup = {}
    buyers = defaultdict(list)
    display_names = {}
    for source, data in sources:
        lookup[source["id"]] = _compact_evidence(source)
        name = _short(source.get("domain") or data.get("competitor"))
        if not name:
            continue
        name = aliases.get(name.casefold(), name)
        key = name.casefold()
        display_names.setdefault(key, name)
        buyers[key].append((source, data))
    if own_source:
        lookup[own_source[0]["id"]] = _compact_evidence(own_source[0])
    keys = sorted(buyers, key=lambda key: (
        -max(_QUALITY[source["scope"]] for source, _ in buyers[key]), display_names[key].casefold(),
    ))[:max_competitors]
    own_facts = _facts([own_source] if own_source else [], as_of, offer, product)
    competitor_facts = {key: _facts(buyers[key], as_of, offer, product) for key in keys}
    rows = []
    compact_candidates = []
    opportunities = []
    for feature, label in FEATURES.items():
        own = _cell(own_facts.get(feature, []))
        competitors = {}
        for key in keys:
            name = display_names[key]
            current = _cell(competitor_facts[key].get(feature, []))
            comparison = _relation(feature, own, current)
            competitors[name] = current | {"comparison": comparison}
            ids = list(dict.fromkeys(own["evidence_ids"] + current["evidence_ids"]))
            compact_candidates.append((feature, name, own, current, comparison, ids))
            if feature in _UX and comparison["status"] == "disadvantage":
                opportunities.append({
                    "feature": feature, "competitor": name, "kind": "hypothesis",
                    "evidence_ids": ids, "next_check": "Проверить влияние различия на сценарий пользователей.",
                })
        rows.append({"feature": feature, "label": label, "own": own, "competitors": competitors})

    priority = {state: index for index, state in enumerate((
        "conflict", "disadvantage", "advantage", "incomparable", "different", "match", "stale",
        "unverified", "undated", "unknown",
    ))}
    compact_candidates.sort(key=lambda row: (priority[row[4]["status"]], not row[5]))
    comparisons, selected = [], {}
    payload_bytes = 0
    research_needed = []
    relevant_gaps = {"conflict", "incomparable", "stale", "unverified", "undated", "unknown"}
    for feature, name, own, current, verdict, ids in compact_candidates:
        if verdict["status"] in relevant_gaps and len(research_needed) < 16:
            research_needed.append({
                "feature": feature, "competitor": name, "reason": verdict["status"],
                "unknown_sides": [side for side, cell in (("own", own), ("competitor", current))
                                  if cell["status"] == "unknown"],
            })
        if not ids or len(comparisons) >= 20 or len(set(selected) | set(ids)) > max_sources:
            continue
        comparison = {
            "feature": feature, "competitor": name, "own": own["value"], "observed": current["value"],
            "as_of": {"own": own["as_of"], "competitor": current["as_of"]},
            "status": verdict["status"], "assessment": verdict, "evidence_ids": ids,
        }
        if feature == "price":
            comparison["conditions"] = {"own": own.get("conditions"), "competitor": current.get("conditions")}
        if current["alternatives"]:
            comparison["alternatives"] = [
                {field: entry[field] for field in ("value", "as_of", "status", "fact_evidence_ids")}
                for entry in current["alternatives"]
            ]
        extras = {key: lookup[key] for key in ids if key not in selected}
        serialized = json.dumps([comparison, list(extras.values())], ensure_ascii=False)
        size = len(serialized.encode("utf-8"))
        if payload_bytes + size > 12_000:
            continue
        payload_bytes += size
        selected.update(extras)
        comparisons.append(comparison)
    return {
        "matrix": {
            "as_of": as_of.isoformat(), "competitors": [display_names[key] for key in keys], "rows": rows,
            "omitted_competitors": len(buyers) - len(keys),
        },
        "evidence_index": lookup,
        "qwen_context": {
            "as_of": as_of.isoformat(), "evidence": list(selected.values()), "comparisons": comparisons,
            "research_needed": research_needed,
            "ux_hypotheses": [entry for entry in opportunities if set(entry["evidence_ids"]) <= selected.keys()][:10],
        },
    }
