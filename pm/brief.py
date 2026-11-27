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
HISTORICAL = {"historical_metric", "historical_comparison", "historical_release",
              "historical_competitor", "historical_document", "memory_context"}


def release_brief(run_id, *, read_run=ai_store.run):
    """Read a saved Core report. Opening the brief does not rerun inference."""
    return report_view(read_run(run_id))


def report_view(report, *, focus=None):
    evidence = report["evidence"]
    hypotheses = report["hypotheses"]
    selected = [item for item in evidence if item["kind"] in {
        "orders", "funnel", "historical_metric", "historical_comparison", "effect",
        "historical_release", "historical_competitor", "memory_context",
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
        "historical_document",
    } and (not focus or item["kind"] in focus)]
    market_focus = focus and "competitor_web" in focus
    effect_focus = focus and "effect" in focus
    plan_focus = focus and "effect_plan" in focus
    offer_check = offer_comparison(evidence) if not (effect_focus or plan_focus) else None
    effect = next((item for item in selected if item["kind"] == "effect"), None)
    reviewed = effect["values"] if effect else {}
    effect_plan = reviewed.get("plan", report["effect_plan"])
    if report["termination"] == "out_of_scope":
        conclusion = report["summary"]
    elif plan_focus:
        conclusion = effect_plan_summary(effect_plan)
    elif effect_focus and not effect:
        conclusion = "Для выбранной задачи оценка эффекта недоступна."
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
    elif focus and focus <= HISTORICAL:
        conclusion = "В выбранной области продуктовой памяти подходящих наблюдений нет."
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
    if plan_focus:
        next_step = effect_plan.get("target_basis") if effect_plan.get("baseline") is not None else None
    elif effect_focus:
        next_step = reviewed.get("recommendation")
    return {
        "run_id": report["run_id"],
        "answer": conclusion,
        "llm_status": report["llm_status"],
        "data_mode": effect["data_mode"] if effect_focus and effect else report["data_mode"],
        "notice": report.get("notice", ""),
        "measurements": measurements,
        "observations": observations,
        "sources": evidence,
        "sources_status": report["sources_status"],
        "offer_comparison": offer_check,
        "findings": report["findings"],
        "hypotheses": hypotheses,
        "next_step": next_step,
        "effect_plan": effect_plan,
        "show_effect_plan": bool(plan_focus or effect or effect_plan.get("baseline_period")),
        "expand_effect_plan": bool(plan_focus),
        "limitations": report["limitations"],
        "trace": [event for event in report["trace"] if event["step"] not in HIDDEN_PHASES],
        "drafts": report["proposed_tasks"],
        "usage": report["usage"],
        "seconds": report["seconds"],
    }


def effect_plan_summary(plan):
    weeks = plan.get("observation_weeks", 4)
    label = "Индекс заказов за четыре полные недели" if weeks == 4 else "Индекс заказов после исправления"
    if plan.get("baseline") is None:
        return f"{label}. Сначала соберите данные базового периода."
    return f"{label} сравните с базой {plan['baseline']:g}."


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
    elif kind == "historical_release":
        delta = values["delta"]
        text = f"Релиз {item['ref']} · {item['observed_at']}"
        if delta is None:
            text += ". Замер по полным окнам недоступен."
        else:
            text += f" · изменение {delta:+g} {item['unit'] or ''}. Сравнение описательное."
    elif kind == "historical_competitor":
        date = item.get("observed_at") or ""
        text = f"{item['ref']} · {values['before']:g} → {values['after']:g} {item['unit']} {date}".strip()
    elif kind == "memory_context":
        decision = {"accept": "Согласовано", "reject": "Отклонено"}.get(values.get("decision"), "Исследовано")
        text = f"{decision} · {item['text']}"
        if values.get("reason"):
            text += f" · {values['reason']}"
        if values.get("effect") in EFFECT_LABELS:
            text += f" · {EFFECT_LABELS[values['effect']]}"
    else:
        text = f"{item['ref']} · {EFFECT_LABELS.get(values['status'], 'Статус оценки недоступен')}."
        if values.get("kaspersky") is not None:
            text += f" Основной индекс {values['kaspersky']:g}"
            if values.get("target") is not None:
                text += f" при цели {values['target']:g}"
            text += "."
    return {"evidence_id": item["id"], "text": text}
