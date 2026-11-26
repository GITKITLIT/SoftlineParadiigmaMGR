"""Build a grounded report, falling back to repeatable server-side analysis."""
import os
import time
from uuid import uuid4

from . import ai_store, qwen
from .analysis_context import collect
from .service import overview, recalculate


def analyze(request, *, collector=None, infer=qwen.infer, persist=True):
    started = time.perf_counter()
    query = request.get("query", "")
    config = request.get("config", {})
    if not isinstance(query, str) or not 1 <= len(query.strip()) <= 500 or not isinstance(config, dict):
        raise ValueError("Введите вопрос до 500 символов и корректные настройки")
    snapshot = recalculate(overview(), config, allow_partial=True)
    partial_sales = any(
        item["before"] is None or item["after"] is None for item in snapshot["series"]
    )
    mode, evidence, statuses, limits, matches = collect(query, snapshot, request, collector=collector)
    if partial_sales:
        statuses["sales"] = {"status": "partial", "reason": "missing_weeks"}
        limits.append("В запрошенных периодах есть пропуски. Показатели с пропусками исключены из сравнения.")
    gathered = time.perf_counter() - started
    findings = []
    for item in evidence:
        delta = item.get("values", {}).get("delta")
        if item["kind"] == "orders" and delta is not None:
            findings.append(dict(
                text=f'{item["ref"]} — изменение среднего индекса {delta:+.1f}%',
                evidence_ids=[item["id"]], contradictions=[], missing=[],
                confidence="medium",
            ))
        if item["kind"] == "funnel" and item["values"]["rate"] is not None:
            values = item["values"]
            findings.append(dict(
                text=f'{item["filter"]["segment"]} · выбор опции {values["rate"]:.1f}%, покрытие {values["days"]}/{values["total"]} дней',
                evidence_ids=[item["id"]], contradictions=[],
                missing=["Есть непокрытые дни"] if item["quality"] == "incomplete" else [],
                confidence="low" if item["quality"] == "incomplete" else "medium",
            ))
    status, hypotheses, usage, reason = infer(query, evidence)
    core = next((item for item in evidence if item["kind"] == "orders"), None)
    control = next((item for item in evidence if item["id"] == "orders:1"), None)
    if core is None or core["values"]["delta"] is None:
        summary = "Недостаточно недель для сравнения основного показателя."
    elif core["values"]["delta"] <= -snapshot["config"]["alert_percent"] and core["values"]["delta"] < 0:
        summary = "Снижение среднего индекса достигло порога. Причина требует проверки."
    else:
        summary = "Снижение среднего индекса не достигло заданного порога."
    if not hypotheses and core and core["values"]["delta"] is not None and core["values"]["delta"] < 0:
        hypotheses = [dict(
            problem="Снижение индекса заказов", cause="Изменение поведения или измерения требует проверки",
            verify="Сопоставить шаги покупки и регистрацию событий по сегментам до изменения",
            expected="Проверка отделит ошибку измерения от потерь на этапе покупки",
            missing="Нужны наблюдения по шагам воронки", risk="Изменение состава покупателей",
            evidence_ids=[core["id"]], contradictions=[], confidence="low",
        )]
    effect_plan = dict(
        primary_kpi="orders_index_4w",
        baseline=core["values"]["rolling_base"] if core else None,
        target=None, guardrails=[],
        control_baseline=control["values"]["rolling_base"] if control else None,
        observation_weeks=4,
        stop_condition="Пересмотреть гипотезу при отсутствии улучшения или ухудшении контрольного показателя.",
    )
    proposals = [
        dict(id=f"proposal-{i + 1}", title=item["problem"], hypothesis=item["cause"],
             verification=item["verify"], expected=item["expected"], risks=item["risk"],
             segment=core["ref"] if core else "Не выбран", evidence_ids=item["evidence_ids"],
             tag=ai_store.TASK_TAG, priority={
                 "impact": "Проверка потерь заказов" if core else "Уточнение проблемы",
                 "evidence_strength": item["confidence"], "effort": "unknown",
                 "dependencies": [item["missing"]],
             })
        for i, item in enumerate(hypotheses)
    ]
    elapsed = time.perf_counter() - started
    report = dict(
        run_id=uuid4().hex, llm_status=status, data_mode=mode, sources_status=statuses,
        evidence=evidence, summary=summary, findings=findings, hypotheses=hypotheses,
        recommended_actions=[h["verify"] for h in hypotheses] or ["Проверить полноту входных данных."],
        backlog_matches=matches, proposed_tasks=proposals, effect_plan=effect_plan, limitations=limits,
        trace=[dict(step="collect", seconds=round(gathered, 3)),
               dict(step="qwen", seconds=round(elapsed - gathered, 3), status=status, reason=reason)],
        usage=usage, seconds=round(elapsed, 3), checks=["checkout", "tracking"],
        hits=[item for item in evidence if item["kind"] in
              {"research", "interview", "competitor_local", "competitor_web"}],
        mode="Локальная Qwen" if status == "ok" else "Расчёт и поиск",
        model=os.getenv("PM_LLM_MODEL", "qwen3-4b") if status == "ok" else None,
        notice=("Модель не подключена или ответ не прошёл проверку. Показан расчёт и поиск."
                if status != "ok" else ""),
    )
    if status == "ok" and hypotheses:
        report.update(hypothesis=hypotheses[0]["cause"], proposal=hypotheses[0]["verify"],
                      refs=hypotheses[0]["evidence_ids"])
    if persist:
        ai_store.save_run(report)
    return report
