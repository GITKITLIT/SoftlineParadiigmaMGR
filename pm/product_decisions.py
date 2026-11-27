"""Turn investigative findings into traceable decisions with conservative defaults."""

from .research import tokens

GENERIC_TERMS = tokens("проверить задача сделать гипотеза проверить сопоставить уточнить")


def task_proposals(hypotheses, matches, evidence, core, plan, *, backlog_status):
    proposals = []
    by_id = {item["id"]: item for item in evidence}
    for i, item in enumerate(hypotheses):
        terms = tokens(item["problem"] + " " + item["verify"]) - GENERIC_TERMS
        cited = {by_id[ref]["ref"] for ref in item["evidence_ids"]
                 if ref in by_id and by_id[ref]["kind"] == "backlog"}
        existing = []
        for match in matches:
            overlap = terms & (tokens(match.get("title") or "") - GENERIC_TERMS)
            if len(overlap) >= 2 or (overlap and match["id"] in cited):
                existing.append(match)

        decision = dict(
            id=f"proposal-{i + 1}", title=item["problem"], hypothesis=item["cause"],
            verification=item["verify"], expected=item["expected"], risks=item["risk"],
            segment=core["ref"] if core else "Не выбран",
            evidence_ids=item["evidence_ids"], contradictions=item["contradictions"],
            criteria=[item["verify"], "Зафиксировать цель и окно наблюдения до начала работ."],
            alternatives=item["alternative_explanations"],
            plan=plan,
            action="review_existing_priority" if existing else "propose_new_task",
            existing_tasks=existing,
            backlog_verified=backlog_status in {"ok", "missing"},
            priority={
                "impact": item["business_impact"],
                "evidence_strength": item["confidence"],
                "effort": "unknown",
                "dependencies": [item["missing"]],
                "reason": "Проверить критерии и приоритет связанной задачи перед созданием новой."
                          if existing else "Сначала подтвердить причину и оценить объём доработки.",
            },
        )
        proposals.append(decision)
    return proposals
