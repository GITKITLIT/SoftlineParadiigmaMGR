"""Execute a bounded, read-only source investigation before synthesis."""
import time

from .analysis_context import FALLBACK

MAX_MODEL_STEPS = 8
PLAN_SECONDS = 60
FINISH_PURPOSES = {"enough_evidence", "insufficient_evidence", "out_of_scope"}
TOOL_PURPOSES = {"measure", "compare", "corroborate", "challenge", "check_duplicate"}
REQUESTED = {
    "sales": ("заказ", "продаж", "контрол", "аномали", "индекс"),
    "funnel": ("воронк", "достижен"),
    "releases": ("релиз", "выпуск"),
    "research": ("интервью", "custdev", "исследован", "пользовател"),
    "competitors": ("конкурент",),
    "backlog": ("бэклог", "задач", "дубликат"),
    "metrika": ("метрик", "визит", "посещаемост"),
    "public_competitors": ("публичн", "интернет"),
    "effect": ("эффект", "итог доработ", "результат исправлен"),
}


def requested_tools(query):
    question = query.casefold()
    return {tool for tool, stems in REQUESTED.items() if any(stem in question for stem in stems)}


def off_topic(query):
    text = query.strip().casefold()
    return not requested_tools(text) and text.startswith((
        "как испечь ", "напиши стих", "расскажи анекдот", "дай рецепт ", "переведи фразу ",
    ))


def valid_action(action, available):
    if not isinstance(action, dict) or set(action) != {"tool", "arguments", "purpose"} or action["arguments"] != {}:
        return False
    tool, purpose = action["tool"], action["purpose"]
    if not isinstance(tool, str) or not isinstance(purpose, str):
        return False
    return purpose in FINISH_PURPOSES if tool == "finish" else tool in available and purpose in TOOL_PURPOSES


def investigate(context, decide=None, *, clock=time.monotonic):
    trace, usage = [], {}
    end = clock() + PLAN_SECONDS
    termination = "offline_collection" if decide is None else "step_limit"

    def execute(name, arguments, purpose, actor):
        started = clock()
        result = context.run(name, arguments)
        trace.append(dict(step="tool", tool=name, arguments=arguments, purpose=purpose, actor=actor,
                          seconds=round(clock() - started, 3), **result))

    required = requested_tools(context.query) & context.available.keys()
    if decide:
        for _ in range(MAX_MODEL_STEPS):
            if clock() >= end:
                termination = "planning_timeout"
                break
            remaining = {name: desc for name, desc in context.available.items() if name not in context.completed}
            if not remaining:
                termination = "sources_exhausted"
                break
            started = clock()
            action, spent = decide(context.query, remaining, list(context.evidence.values()), trace, end)
            for key, count in spent.items():
                usage[key] = usage.get(key, 0) + count
            seconds = round(clock() - started, 3)
            if not valid_action(action, remaining):
                trace.append(dict(step="decision", actor="qwen", status="invalid_or_unavailable", seconds=seconds))
                termination = "decision_unavailable"
                break
            if action["tool"] == "finish":
                purpose = action["purpose"]
                if required - context.completed or (purpose == "out_of_scope" and required):
                    trace.append(dict(step="decision", actor="qwen", status="premature_stop", seconds=seconds))
                    # Complete explicitly requested local checks. Unrequested tools are still
                    # optional. Selected external scopes require a subsequent model choice.
                    outstanding = [name for name in FALLBACK if name in required - context.completed
                                   and name not in {"metrika", "public_competitors"}]
                    if outstanding:
                        execute(outstanding[0], {}, "requested_coverage", "server")
                        continue
                    termination = "decision_unavailable"
                    break
                termination = purpose
                trace.append(dict(step="decision", actor="qwen", status=termination, seconds=seconds))
                break
            trace.append(dict(step="decision", actor="qwen", status="selected", seconds=seconds))
            execute(action["tool"], action["arguments"], action["purpose"], "qwen")
    if termination in {"offline_collection", "decision_unavailable", "planning_timeout", "step_limit"}:
        # A failed planner never prevents a reproducible local investigation. Once part
        # of a run is model-driven, fallback does not initiate new network requests.
        for tool in FALLBACK:
            if tool not in context.available or tool in context.completed:
                continue
            if decide and tool in {"metrika", "public_competitors"}:
                continue
            execute(tool, {}, "deterministic_fallback", "server")
    return trace, usage, termination
