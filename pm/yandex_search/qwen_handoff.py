"""Bounded competitive evidence for AI Core's local Qwen call."""

import json
import re
from html import unescape

from .parsing import plain_text

_MAX_EXCERPT = 160
_UNSAFE = re.compile(
    r"<[!?/A-Za-z][^>]*>|"
    r"(?:ignore|override|disregard).{0,60}(?:instruction|prompt|system)|"
    r"(?:игнорируй|игнорируйте|забудь|выполни).{0,60}(?:инструкц|промпт|команд)|"
    r"(?:system|assistant|developer)\s*:", re.I,
)
_TYPES = {
    "public_page": "page_excerpt", "search_snippet": "snippet",
    "local_study": "local_study", "internal_offer": "internal_offer",
}


def _unsafe(value):
    return isinstance(value, str) and bool(_UNSAFE.search(unescape(value)))


def _quote(value):
    if not isinstance(value, str) or _unsafe(value):
        return None
    unescaped = unescape(value)
    return plain_text(unescaped, _MAX_EXCERPT) or None


def _source(item):
    return {
        "id": item["id"], "ref": item["ref"], "type": _TYPES.get(item["scope"], "unknown"),
        "date": item.get("as_of"), "quote": _quote(item.get("excerpt")),
    }


def _encoded_size(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8"))


def pack_competitor_context(context, *, changes=None, max_bytes=4500):
    """Pack claims and their complete citation sets within a UTF-8 budget, without an LLM call."""
    if type(max_bytes) is not int or not 1000 <= max_bytes <= 6500:
        raise ValueError("invalid_qwen_handoff_budget")
    current = context["qwen_context"]
    lookup = context["evidence_index"]
    output = {"schema_version": 1, "as_of": current["as_of"], "facts": [], "changes": [],
              "sources": [], "unknowns": [], "ux_hypotheses": []}
    included = set()

    def add(section, row, sources):
        new = [_source(sources[identifier]) for identifier in dict.fromkeys(row.get("evidence_ids", ()))
               if identifier not in included]
        output[section].append(row)
        output["sources"].extend(new)
        if _encoded_size(output) > max_bytes:
            output[section].pop()
            if new:
                del output["sources"][-len(new):]
            return
        included.update(source["id"] for source in new)

    temporal = sorted((changes or {}).get("changes", []),
                      key=lambda row: (not row["material"], row["competitor"], row["feature"]))
    for item in temporal[:3]:
        if any(_unsafe(value) for value in (item["competitor"], item["before"]["value"], item["after"]["value"])):
            continue
        relevant = item.get("before_sources", []) + item.get("after_sources", [])
        historical = {record["id"]: record for record in relevant}
        if not historical:
            continue
        add("changes", {
            "feature": item["feature"], "competitor": item["competitor"], "state": item["reason"],
            "before": {field: item["before"][field] for field in ("value", "status", "as_of")},
            "after": {field: item["after"][field] for field in ("value", "status", "as_of")},
            "price_delta": item.get("price_delta"), "evidence_ids": list(historical),
        }, historical)

    for item in current["comparisons"]:
        alternatives = item.get("alternatives", [])
        if any(_unsafe(value) for value in (
            item["competitor"], item["own"], item["observed"], *(alt["value"] for alt in alternatives),
        )):
            continue
        ids = item["evidence_ids"]
        add("facts", {
            "feature": item["feature"], "competitor": item["competitor"], "state": item["status"],
            "own": item["own"], "observed": item["observed"], "dates": item["as_of"],
            "details": {key: item["assessment"][key] for key in (
                "different_terms", "unknown_terms", "competitor_minus_own", "currency",
            ) if key in item["assessment"]},
            "alternatives": alternatives, "evidence_ids": ids,
        }, lookup)

    for gap in current["research_needed"][:6]:
        if not _unsafe(gap["competitor"]):
            add("unknowns", gap, lookup)
    for hypothesis in current["ux_hypotheses"][:2]:
        if not _unsafe(hypothesis["competitor"]):
            add("ux_hypotheses", hypothesis | {"type": "unconfirmed_hypothesis"}, lookup)
    return output
