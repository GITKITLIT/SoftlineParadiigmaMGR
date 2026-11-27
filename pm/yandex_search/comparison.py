"""Prepare grounded local comparison context for a downstream analyst or Qwen."""

import re
from datetime import date
from decimal import Decimal

from pm.competitor_sources import _read_metadata

_UNKNOWN = {"-", "—", "н/д", "n/a", "неизвестно", "unknown"}
_TERMS = ("product", "version", "license_type", "duration", "devices", "tax", "currency")
_UX = ("license_selection", "purchase_flow", "consultation", "availability")
_CURRENCIES = {
    "руб": "rub", "руб.": "rub", "₽": "rub",
    "rub": "rub", "usd": "usd", "eur": "eur", "kzt": "kzt", "byn": "byn",
}
_TAX = {
    "включён": "included", "включен": "included", "с ндс": "included", "included": "included",
    "не включён": "excluded", "не включен": "excluded", "excluded": "excluded",
    "освобождение от ндс": "exempt", "exempt": "exempt",
}


def _text(value):
    if type(value) in (int, float):
        value = str(value)
    return " ".join(value.split())[:500] if isinstance(value, str) else ""


def _offer(row):
    values = {key: _text(row.get(key)).casefold() for key in (*_TERMS, "price")}
    for key, value in values.items():
        if value in _UNKNOWN:
            values[key] = ""
    values["currency"] = _CURRENCIES.get(values["currency"], "")
    values["tax"] = _TAX.get(values["tax"], "")
    price = values["price"].replace(" ", "").replace(",", ".")
    values["price"] = price if re.fullmatch(r"[0-9]{1,12}(?:\.[0-9]{1,4})?", price) else ""
    if not re.fullmatch(r"[1-9][0-9]{0,8}", values["devices"]):
        values["devices"] = ""
    return values


def _metadata(item):
    if item["kind"] != "competitor_web":
        return item
    # Recognize explicit labels in a single snippet. Keep other language as evidence text.
    fragments = re.split(r"[;|\n]", item["text"])
    labeled = [fragment.split(":", 1) for fragment in fragments if ":" in fragment]
    return _read_metadata((label.strip(), value.strip()) for label, value in labeled)


def _source(item, scope):
    source = {key: item[key] for key in (
        "id", "ref", "kind", "text", "title", "url", "domain", "topic", "content_type",
        "published_at", "published_date", "modified_at", "modified_date", "retrieved_at",
        "imported_at", "study_date", "provenance",
    ) if key in item}
    source["scope"] = scope
    return source


def _date_alignment(own_date, competitor_date):
    try:
        dates = [date.fromisoformat(value) for value in (own_date, competitor_date)]
    except (TypeError, ValueError):
        return "unknown"
    return "same_day" if dates[0] == dates[1] else "different_days"


def _assessment(ours, theirs, scope, date_alignment):
    relevant = (*_TERMS, "price")
    missing = {
        "own": [key for key in relevant if not ours[key]],
        "competitor": [key for key in relevant if not theirs[key]],
    }
    differences = [key for key in _TERMS if ours[key] and theirs[key] and ours[key] != theirs[key]]
    status = "comparable"
    if differences:
        status = "mismatched_terms"
    elif any(missing.values()):
        status = "insufficient_data"
    elif scope == "search_snippet" or date_alignment != "same_day":
        status = "requires_verification"
    delta = None
    if status == "comparable":
        delta = {
            "amount": format(Decimal(theirs["price"]) - Decimal(ours["price"]), "f"),
            "currency": theirs["currency"].upper(),
        }
    return {
        "status": status, "missing": missing, "different_terms": differences,
        "date_alignment": date_alignment, "competitor_minus_own": delta,
    }


def build_comparison_context(*, web_result: dict, local_evidence: list[dict],
                             own_evidence: dict | None = None) -> dict:
    """Build observations using v1 results and optionally an explicit internal offer."""
    if own_evidence is not None and (
        own_evidence.get("kind") in {"competitor_web", "Конкуренты"}
        or own_evidence.get("content_type") in {"snippet", "page_excerpt"}
        or not own_evidence.get("id") or not own_evidence.get("ref")
    ):
        raise ValueError("own_evidence_requires_internal_source")
    own_terms = _offer(own_evidence or {})
    own_date = (own_evidence or {}).get("study_date")
    own = None if own_evidence is None else {
        "source": _source(own_evidence, "internal_offer"),
        "offer": {key: value or None for key, value in own_terms.items()},
    }
    observations = []
    sources = [(item, "local_study") for item in local_evidence if item["kind"] == "Конкуренты"]
    sources.extend((item, "search_snippet" if item["content_type"] == "snippet" else "public_page")
                   for item in web_result["items"] if item["kind"] == "competitor_web")
    for item, scope in sources:
        data = _metadata(item)
        terms = _offer(data)
        study_date = data.get("study_date")
        source = _source(item, scope)
        if study_date:
            source["study_date"] = study_date
        comparison = _assessment(own_terms, terms, scope, _date_alignment(own_date, study_date))
        observations.append({
            "source": source,
            "competitor": data.get("competitor") or item.get("domain"),
            "offer": {key: value or None for key, value in terms.items()},
            "ux": {key: _text(data.get(key)) or None for key in _UX},
            "price_comparison": comparison,
            "hypothesis": None,
            "verification": (
                "Проверить условия в публичном первоисточнике на дату анализа."
                if scope == "search_snippet" else
                "Подтвердить дату, тариф и условия лицензии перед выводом."
            ),
        })
    return {
        "own": own,
        "observations": observations,
        "search": {key: web_result[key] for key in ("status", "reason", "retrieved_at", "warnings")},
    }
