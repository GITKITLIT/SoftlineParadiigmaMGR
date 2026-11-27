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
HISTORICAL_WINDOWS = re.compile(r"год назад|прошл\w+ год|\bквартал\w*\b|\b20\d\d\b.+\b20\d\d\b", re.I)
MEASUREMENT_FOCUS = {
    "compare_history": {"historical_metric", "historical_comparison"},
    "product_history": {"release", "historical_metric", "historical_comparison"},
    "search_history": {"historical_document", "memory_context"},
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
    follow_up = mode == "follow_up" or (
        mode == "auto" and not off_topic(question) and bool(FOLLOW_UP.search(question))
    )
    prior = previous if follow_up else None
    effective_scope = ((prior or {}).get("scope", {}) | scope)
    effective_scope = {key: value for key, value in effective_scope.items() if value is not None}
    query = question
    hypothesis = None
    old_report = (prior or {}).get("report") or {}
    evidence_question = bool(COUNTEREVIDENCE.search(question) or SUPPORT.search(question))
    if prior and not off_topic(question):
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
    memory = effective_scope.get("memory", {})
    turn = {
        "question": question,
        "scope": effective_scope,
        "previous_run_id": prior["report"]["run_id"] if prior and prior.get("report") else None,
    }
    if not prior and follow_up and not scope:
        return turn | {"report": None, "reply": {"answer": "Уточните, к какому вопросу или гипотезе вы возвращаетесь."}}
    if HISTORICAL_WINDOWS.search(question) and not (memory.get("compare") or memory.get("product") or memory.get("competitors")):
        return turn | {"report": None, "reply": {
            "answer": "Укажите исторический ряд и два периода для сравнения.",
            "needs": "historical_windows",
        }}
    if "effect" in requested_tools(question) and not effective_scope.get("task_id"):
        return turn | {"report": None, "reply": {
            "answer": "Выберите согласованную задачу, эффект которой нужно оценить.",
            "needs": "accepted_task",
        }}
    report = analyze({"query": query, **effective_scope}, persist=False, progress=progress)
    asked = requested_tools(question)
    focus = set()
    if memory.get("compare"):
        focus |= MEASUREMENT_FOCUS["compare_history"]
    if not re.search(r"почему|гипотез|провер|что делать|после релиз", question, re.I):
        if asked & {"compare_history", "search_history", "effect", "competitors", "backlog", "research", "funnel"}:
            asked = asked - {"sales", "releases"}
        for tool in asked:
            focus |= MEASUREMENT_FOCUS.get(tool, set())
    view = report_view(report, focus=focus)
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
