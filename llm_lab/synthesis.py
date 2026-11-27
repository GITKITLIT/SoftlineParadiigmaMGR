"""Prepare qualitative cues for the hypothesis-only AI Core output contract."""

import re

from .offer_evidence import offer_comparison

NUMERAL = re.compile(r"[+−-]?[\w-]*\d[\w-]*(?:[.,:%]\d+)*%?")
DIRECTIONS = {
    "delta": "изменение",
    "delta_percent": "изменение",
    "control_delta": "изменение контрольного ряда",
    "primary_delta": "изменение основного ряда",
    "difference_pp": "разность с контролем",
    "orders_delta": "изменение заказов",
    "metrika_delta": "изменение Метрики",
}


def text(value):
    return " ".join(NUMERAL.sub(" ", value).split()) if isinstance(value, str) else value


def qualitative_values(values):
    result = {}
    for key, value in (values or {}).items():
        if key in DIRECTIONS and (value is None or type(value) in (int, float)):
            result[DIRECTIONS[key]] = (
                "неизвестно" if value is None else "снижение" if value < 0 else "рост" if value > 0 else "без изменений"
            )
        elif key in {"related_task", "status", "date", "release_id"}:
            result[key] = text(value)
    return result


def qualitative_input(question, evidence, facts):
    rows = []
    for source in evidence:
        row = {key: text(source[key]) for key in (
            "kind", "ref", "text", "quality", "data_mode", "source_group", "metric", "topic", "freshness",
        ) if key in source}
        row["id"] = source["id"]
        if isinstance(source.get("filter"), dict) and source["filter"].get("segment"):
            row["segment"] = text(source["filter"]["segment"])
        values = qualitative_values(source.get("values"))
        if values:
            row["cues"] = values
        rows.append(row)
    observations = []
    for fact in facts:
        row = {key: fact[key] for key in ("relation", "statement_type", "evidence_ids", "contradictions") if key in fact}
        row["missing"] = [text(item) for item in fact.get("missing", [])]
        values = qualitative_values(fact.get("values"))
        if values:
            row["cues"] = values
        observations.append(row)
    comparison = offer_comparison(evidence)
    if comparison:
        observations.append({
            "relation": "price_comparability", "statement_type": "fact",
            "evidence_ids": comparison["evidence_ids"], "status": comparison["status"],
            "observation": comparison["text"], "missing": comparison["missing"],
        })
    return {"question": text(question), "evidence": rows, "server_observations": observations}
