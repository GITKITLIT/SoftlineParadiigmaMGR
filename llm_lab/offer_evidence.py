"""Classify whether normalized v1 evidence supports a price comparison."""

from collections import defaultdict


def offer_comparison(evidence):
    public = [row for row in evidence if row["kind"] == "competitor_web"]
    if not public:
        return None
    offers = [row for row in public if row["quality"] == "page_excerpt" and row.get("offer")]
    current = [row for row in offers if row.get("freshness") == "recent"]
    groups = defaultdict(set)
    for row in current:
        offer = row["offer"]
        groups[offer["currency"], offer["terms"]].add(row["competitor"])
    if any(len(domains) >= 2 for domains in groups.values()):
        status = "comparable"
        conclusion = "Есть свежие проверенные предложения с совпадающими условиями и валютой."
        missing = []
    elif len({row["competitor"] for row in current}) >= 2:
        status = "different_terms"
        conclusion = "У проверенных предложений различаются условия или валюта. Сопоставимая пара цен отсутствует."
        missing = ["Нужна пара предложений с одинаковыми условиями покупки."]
    else:
        status = "insufficient"
        conclusion = "Данных недостаточно для сравнения цен. Нужны свежие первоисточники с одинаковыми условиями."
        missing = ["Сверить редакцию, тип и срок лицензии, устройства, налоги, валюту и дату."]
    return {"status": status, "text": conclusion, "evidence_ids": [row["id"] for row in public], "missing": missing}
