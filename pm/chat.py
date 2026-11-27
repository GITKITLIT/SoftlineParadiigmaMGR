"""Route bounded conversational turns through the product investigation."""

import re

from . import investigation
from .agent import off_topic, requested_tools
from .brief import report_view
from .memory import redact

MAX_QUESTION = 500
SCOPE_KEYS = ("config", "memory", "metrika", "web", "task_id")
FOLLOW_UP = re.compile(
    r"^(?:а\s+)?(?:почему это|что (?:ей|ему|этому|им) противоречит|"
    r"чем это|где это|как это|что ещё|что еще|что проверить|как измерить|"
    r"какие доказательства подтверждают|какие данные противоречат)|"
    r"\b(?:эт[уаой]|предыдущую|первую|вторую) гипотез\w*",
    re.IGNORECASE,
)
COUNTEREVIDENCE = re.compile(r"противореч", re.I)
SUPPORT = re.compile(r"доказательств|подтвержд", re.I)
PLAN_EFFECT = re.compile(r"как\s+(?:измерить|оценить|посчитать)\s+эффект", re.I)
HISTORICAL_WINDOWS = re.compile(r"год назад|прошл\w+ год|\bквартал\w*\b|\b20\d\d\b.+\b20\d\d\b", re.I)
MEMORY_LOOKUP = re.compile(
    r"похож\w*\s+(?:проблем|аномал|расследован)|повторяющ\w*\s+(?:пользовательск\w*\s+)?проблем|"
    r"(?:проверенн|отклон[её]нн)\w*\s+гипотез|"
    r"(?:принял|отклонил)\w*.+(?:стар|предыдущ)\w*\s+задач|"
    r"(?:эффект|результат)\w*.+(?:стар|предыдущ)\w*\s+рекомендац",
    re.I,
)
MEMORY_TOOLS = {
    "compare": "compare_history", "search": "search_history",
    "competitors": "competitor_history", "product": "product_history",
}
MEASUREMENT_FOCUS = {
    "compare_history": {"historical_metric", "historical_comparison"},
    "product_history": {"historical_release"},
    "search_history": {"historical_document", "memory_context"},
    "competitor_history": {"historical_competitor"},
    "competitors": {"competitor_local", "competitor_web", "historical_competitor"},
    "public_competitors": {"competitor_web"},
    "backlog": {"backlog"},
    "effect": {"effect"},
    "research": {"research", "interview"},
    "releases": {"release"},
    "funnel": {"funnel"},
    "sales": {"orders"},
}


def normalize_turn(body):
    question = body.get("question", "")
    if not isinstance(question, str) or not 1 <= len(question.strip()) <= MAX_QUESTION:
        raise ValueError("Введите вопрос длиной до 500 символов.")
    mode = body.get("mode", "auto")
    if not isinstance(mode, str) or mode not in {"auto", "follow_up", "new_topic"}:
        raise ValueError("Выберите продолжение разговора или новый вопрос.")
    scope = body.get("scope", {})
    if not isinstance(scope, dict) or scope.keys() - set(SCOPE_KEYS):
        raise ValueError("Проверьте область расследования.")
    for key, value in scope.items():
        expected = str if key == "task_id" else dict
        if value is not None and not isinstance(value, expected):
            raise ValueError("Проверьте область расследования.")
    return redact(question.strip()), mode, scope


def consult(body, *, previous=None, analyze=investigation.analyze, progress=None):
    """Return the private turn and its presentation for the calling session store."""
    question, mode, scope = normalize_turn(body)
    topical = not off_topic(question)
    asks_for_plan = bool(PLAN_EFFECT.search(question))
    follow_up = mode == "follow_up" or (
        mode == "auto" and topical and (asks_for_plan or bool(FOLLOW_UP.search(question)))
    )
    prior = previous if follow_up else None
    effective_scope = ((prior or {}).get("scope", {}) | scope)
    effective_scope = {key: value for key, value in effective_scope.items() if value is not None}
    memory = effective_scope.get("memory", {})
    if MEMORY_LOOKUP.search(question) and not effective_scope.get("task_id") and not (memory.keys() & MEMORY_TOOLS.keys()):
        memory = memory | {"search": {}}
    historical = memory.keys() & MEMORY_TOOLS.keys()
    if historical:
        effective_scope["memory"] = memory | {"only_history": True}
    asked = requested_tools(question)
    effect_intent = None
    if not historical and topical:
        if effective_scope.get("task_id") and "effect" in asked:
            effect_intent = "review"
        elif asks_for_plan:
            effect_intent = "plan"
    query = question
    hypothesis = None
    old_report = (prior or {}).get("report") or {}
    evidence_question = bool(COUNTEREVIDENCE.search(question) or SUPPORT.search(question))
    if prior and topical:
        context = "\nПредыдущий вопрос: " + prior["question"][:115]
        hypotheses = old_report.get("hypotheses") or []
        if hypotheses and effective_scope == prior.get("scope") and (
            evidence_question or "гипотез" in question.casefold()
        ):
            index = 1 if "втор" in question.casefold() and len(hypotheses) > 1 else 0
            hypothesis = hypotheses[index]
            context += "\nПредыдущая гипотеза для проверки: " + hypothesis["cause"][:100]
        if MAX_QUESTION - len(question) > 32:
            query = (query + context)[:MAX_QUESTION]
    turn = {
        "question": question,
        "scope": effective_scope,
        "previous_run_id": prior["report"]["run_id"] if prior and prior.get("report") else None,
    }
    if not prior and follow_up and not scope and not effect_intent:
        return turn | {"report": None, "reply": {"answer": "Уточните, к какому вопросу или гипотезе вы возвращаетесь."}}
    if HISTORICAL_WINDOWS.search(question) and not historical:
        return turn | {"report": None, "reply": {
            "answer": "Укажите исторический ряд и два периода для сравнения.",
            "needs": "historical_windows",
        }}
    if "effect" in asked and not effective_scope.get("task_id") and not historical and effect_intent != "plan":
        return turn | {"report": None, "reply": {
            "answer": "Выберите согласованную задачу, эффект которой нужно оценить.",
            "needs": "accepted_task",
        }}
    request = {"query": query, **effective_scope}
    if effect_intent:
        request["effect_intent"] = effect_intent
    reuse_plan = effect_intent == "plan" and prior and effective_scope == prior.get("scope") and (
        old_report.get("effect_plan", {}).get("baseline") is not None
    )
    report = old_report if reuse_plan else analyze(request, persist=False, progress=progress)
    focus = set()
    if effect_intent:
        focus.add("effect" if effect_intent == "review" else "effect_plan")
    elif historical:
        for name in historical:
            focus |= MEASUREMENT_FOCUS[MEMORY_TOOLS[name]]
    elif not re.search(r"почему|гипотез|провер|что делать|после релиз", question, re.I):
        if asked & {"compare_history", "search_history", "effect", "competitors", "backlog", "research", "funnel"}:
            asked = asked - {"sales", "releases"}
        for tool in asked:
            focus |= MEASUREMENT_FOCUS.get(tool, set())
    view = report_view(report, focus=focus)
    if reuse_plan:
        view.update(llm_status="not_needed", trace=[], usage={}, seconds=0, drafts=[], hypotheses=[], notice="")
    if turn["previous_run_id"]:
        view["previous_run_id"] = turn["previous_run_id"]
    if hypothesis and evidence_question:
        contrary = bool(COUNTEREVIDENCE.search(question))
        refs = hypothesis["contradictions" if contrary else "evidence_ids"]
        view["previous_evidence"] = [item for item in old_report["evidence"] if item["id"] in refs]
        if contrary:
            view["answer"] = ("В предыдущем ответе указаны противоречащие источники."
                              if refs else "В предыдущем ответе противоречащие источники не указаны.")
        else:
            view["answer"] = "Источники, на которые опиралась предыдущая гипотеза."
    return turn | {"report": report, "reply": view}
