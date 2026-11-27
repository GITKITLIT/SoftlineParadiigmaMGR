"""Compact release and chat views of a single validated AI Core report."""

from llm_lab.offer_evidence import offer_comparison

from . import ai_store


EFFECT_LABELS = {
    "waiting_for_data": "Ожидаются четыре полные недели после исправления",
    "achieved": "Цель достигнута",
    "partial": "Частичное улучшение",
    "not_achieved": "Цель не достигнута",
    "inconclusive": "Для оценки нужны дополнительные данные",
    "stop_condition_met": "Достигнут согласованный порог остановки",
}
HIDDEN_PHASES = {"collect", "qwen"}


def release_brief(run_id, *, read_run=ai_store.run):
    """Read a saved Core report. Opening the brief does not rerun inference."""
    return report_view(read_run(run_id))


def report_view(report, *, focus=None):
    evidence = report["evidence"]
    hypotheses = report["hypotheses"]
    selected = [item for item in evidence if item["kind"] in {
        "orders", "funnel", "historical_metric", "historical_comparison", "effect",
    }]
    if focus:
        selected = [item for item in selected if item["kind"] in focus]
    measurements = [measurement(item) for item in selected]
    if focus and "historical_comparison" in focus:
        index = next((i for i, item in enumerate(selected) if item["kind"] == "historical_comparison"), None)
        if index is not None:
            measurements.insert(0, measurements.pop(index))
    observations = [item for item in evidence if item["kind"] in {
        "research", "interview", "competitor_local", "competitor_web", "backlog", "release",
        "historical_document", "historical_competitor", "memory_context",
    } and (not focus or item["kind"] in focus)]
    offer_check = offer_comparison(evidence)
    market_focus = focus and "competitor_web" in focus
    if report["termination"] == "out_of_scope":
        conclusion = report["summary"]
    elif market_focus and offer_check:
        conclusion = offer_check["text"]
    elif market_focus and report["sources_status"]["competitor_web"]["status"] == "not_checked":
        conclusion = "Публичные источники ещё не проверены в этом расследовании."
    elif market_focus and report["sources_status"]["competitor_web"]["status"] in {"unavailable", "error"}:
        conclusion = "Публичные источники недоступны в этом расследовании."
    elif measurements:
        conclusion = measurements[0]["text"]
    elif observations:
        first = observations[0]
        conclusion = f"{first['ref']} · {first['text'][:150]}" if focus and first.get("text") else (
            f"Доступные наблюдения по вопросу: {len(observations)}."
        )
    else:
        conclusion = "По выбранным источникам данных для ответа недостаточно."
    measurement_gap = any(row.get("relation") == "cart_measurement_gap" for row in report["findings"])
    if not focus and measurement_gap:
        conclusion += " Конверсия в корзину недоступна для части выбранного среза."
    if not focus and hypotheses and report["llm_status"] == "ok" and not measurement_gap:
        conclusion = f"{conclusion} Гипотеза: {hypotheses[0]['cause']}"
    next_step = report["recommended_actions"][0] if hypotheses or measurement_gap else None
    if market_focus and offer_check and offer_check["missing"]:
        next_step = offer_check["missing"][0]
    return {
        "run_id": report["run_id"],
        "answer": conclusion,
        "llm_status": report["llm_status"],
        "data_mode": report["data_mode"],
        "notice": report.get("notice", ""),
        "measurements": measurements,
        "observations": observations,
        "sources": evidence,
        "sources_status": report["sources_status"],
        "offer_comparison": offer_check,
        "findings": report["findings"],
        "hypotheses": hypotheses,
        "next_step": next_step,
        "effect_plan": report["effect_plan"],
        "limitations": report["limitations"],
        "trace": [event for event in report["trace"] if event["step"] not in HIDDEN_PHASES],
        "drafts": report["proposed_tasks"],
        "usage": report["usage"],
        "seconds": report["seconds"],
    }


def measurement(item):
    values = item["values"]
    kind = item["kind"]
    if kind == "orders":
        delta = values["delta"]
        text = (f"{item['ref']}: изменение индекса {delta:+.1f}%."
                if delta is not None else f"{item['ref']}: сравнение индекса недоступно.")
    elif kind == "funnel":
        segment = {"desktop": "компьютеры", "mobile": "телефоны"}.get(
            item["filter"]["segment"], item["filter"]["segment"],
        )
        rate = values["rate"]
        measure = f"{values['num']}/{values['den']} ({rate:.1f}%)" if rate is not None else "доля неизвестна"
        text = f"{item['ref']}, {segment}: {measure}, покрытие {values['days']}/{values['total']} дней."
    elif kind == "historical_metric":
        window = " — ".join(item["period"])
        value = values["value"]
        text = (f"{item['ref']}, {window}: {value:g} {item['unit']}."
                if value is not None else f"{item['ref']}, {window}: полные данные отсутствуют.")
    elif kind == "historical_comparison":
        delta = values["delta_percent"]
        text = (f"{item['ref']}: изменение между историческими окнами {delta:+.1f}%."
                if delta is not None else f"{item['ref']}: относительное изменение недоступно.")
    else:
        text = EFFECT_LABELS.get(values["status"], "Статус оценки недоступен") + "."
    return {"evidence_id": item["id"], "text": text}
