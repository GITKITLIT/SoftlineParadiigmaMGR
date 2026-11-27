"""Build a grounded report, falling back to repeatable server-side analysis."""
import os
import re
import time
from uuid import uuid4

from . import ai_store, qwen
from .memory import redact
from .agent import investigate, off_topic
from .analysis_context import InvestigationContext
from .cross_analysis import correlate, strengthen
from .product_decisions import task_proposals


def analyze(request, *, collector=None, meter=None, decide=None, infer=None,
            snapshot_loader=None, previous=None, persist=True, progress=None):
    started = time.perf_counter()
    query = request.get("query", "")
    config = request.get("config", {})
    if not isinstance(query, str) or not 1 <= len(query.strip()) <= 500 or not isinstance(config, dict):
        raise ValueError("Введите вопрос до 500 символов и корректные настройки")
    query = redact(query)
    request = request | {"query": query}
    context = InvestigationContext(query, request, collector=collector, meter=meter,
                                   snapshot_loader=snapshot_loader, remember=persist)
    planner = decide if decide is not None else qwen.planner() if infer is None else None
    screened = off_topic(query)
    if screened:
        trace, planning_usage, termination = [{"step": "scope", "status": "out_of_scope", "actor": "server"}], {}, "out_of_scope"
    else:
        trace, planning_usage, termination = investigate(context, planner, progress=progress)
    if termination != "out_of_scope" and context.evidence and "backlog" in context.available and "backlog" not in context.completed:
        check = context.run("backlog", {})
        trace.append({"step": "tool", "tool": "backlog", "actor": "server",
                      "purpose": "approval_prerequisite", "arguments": {}, "seconds": 0, **check})
    mode, evidence = context.mode, list(context.evidence.values())
    statuses, limits, matches = context.statuses, context.limits, context.matches
    gathered = time.perf_counter() - started
    findings = correlate(evidence)
    comparison = next((item for item in evidence if item["id"] == "memory:comparison"), None)
    if comparison:
        related = [item["id"] for item in evidence if item["kind"] == "historical_metric"]
        findings.append(dict(
            relation="historical_comparison", statement_type="fact",
            text="Изменение между сопоставимыми историческими окнами",
            evidence_ids=related, contradictions=[], missing=[], checks=[],
            confidence="medium", values=comparison["values"],
        ))
    for item in evidence:
        delta = item.get("values", {}).get("delta")
        if item["kind"] == "orders" and delta is not None:
            findings.append(dict(
                text=f'{item["ref"]} — изменение среднего индекса {delta:+.1f}%',
                evidence_ids=[item["id"]], contradictions=[], missing=[],
                confidence="medium", statement_type="fact", checks=[],
            ))
        if item["kind"] == "funnel" and item["values"]["rate"] is not None:
            values = item["values"]
            findings.append(dict(
                text=f'{item["filter"]["segment"]} · выбор опции {values["rate"]:.1f}%, покрытие {values["days"]}/{values["total"]} дней',
                evidence_ids=[item["id"]], contradictions=[],
                missing=["Есть непокрытые дни"] if item["quality"] == "incomplete" else [],
                confidence="low" if item["quality"] == "incomplete" else "medium",
                statement_type="fact", checks=["Сверить покрытие с журналом сбора."],
            ))
    core = next((item for item in evidence if item["kind"] == "orders"), None)
    control = next((item for item in evidence if item["id"] == "orders:1"), None)
    delta = core["values"]["delta"] if core else None
    measurement_question = query.casefold().startswith(("есть ли снижение ", "оцени масштаб "))
    skip_synthesis = measurement_question and (delta is None or delta >= 0) and not any(
        keyword in query.casefold() for keyword in ("почему", "гипотез", "конкурент", "интервью")
    )
    if termination == "out_of_scope":
        evidence, findings, matches, limits = [], [], [], []
        core, control = None, None
        trace = [{**event, "evidence_ids": []} if event["step"] == "tool" else event for event in trace]
        status, hypotheses, usage, reason = "not_needed" if screened else "ok", [], {}, None
    elif skip_synthesis:
        status, hypotheses, usage, reason = "not_needed", [], {}, "measurement_only"
    elif comparison and not any(word in query.casefold() for word in ("почему", "гипотез", "как проверить")):
        status, hypotheses, usage, reason = "not_needed", [], {}, "historical_measurement"
    elif infer is not None:
        status, hypotheses, usage, reason = infer(query, evidence)
    else:
        if progress:
            progress({"step": "synthesis", "status": "running"})
        status, hypotheses, usage, reason = qwen.infer(query, evidence, planning=False, facts=findings)
    usage = {key: planning_usage.get(key, 0) + usage.get(key, 0) for key in planning_usage | usage}
    if termination == "out_of_scope":
        summary = "Вопрос за пределами продуктовой аналитики."
    elif comparison:
        summary = "Сопоставлены выбранные исторические периоды."
    elif core is None or core["values"]["delta"] is None:
        summary = "Недостаточно недель для сравнения основного показателя."
    elif core["values"]["delta"] <= -core["values"]["alert_percent"] and core["values"]["delta"] < 0:
        summary = "Снижение среднего индекса достигло порога. Причина требует проверки."
    else:
        summary = "Снижение среднего индекса не достигло заданного порога."
    if status == "ok" and core and core["values"]["delta"] is not None and core["values"]["delta"] >= 0:
        spurious = re.compile(r"(паден|снижен|упал).{0,30}заказ|заказ.{0,30}(сниз|упал|паден)", re.I)
        kept = [item for item in hypotheses if not spurious.search(item["problem"])]
        if len(kept) < len(hypotheses):
            limits.append("Отклонена гипотеза, описание которой заявляло падение заказов без сигнала в данных.")
        hypotheses = kept
    if status not in {"ok", "not_needed"} and not hypotheses and core and core["values"]["delta"] is not None and core["values"]["delta"] < 0:
        hypotheses = [dict(
            problem="Снижение индекса заказов", cause="Изменение поведения или измерения требует проверки",
            verify="Сопоставить шаги покупки и регистрацию событий по сегментам до изменения",
            expected="Проверка отделит ошибку измерения от потерь на этапе покупки",
            missing="Нужны наблюдения по шагам воронки", risk="Изменение состава покупателей",
            evidence_ids=[core["id"]], contradictions=[], confidence="low",
        )]
        release = next((row for row in evidence if row["id"] == "release:current"), None)
        if release:
            hypotheses.append(dict(
                problem="Возможный барьер после изменения продукта",
                cause="Изменение могло затронуть переход к оформлению в основной группе",
                verify="Сравнить один и тот же путь покупки для затронутой и незатронутой групп",
                expected="Если барьер связан с изменением, он воспроизводится только на затронутом пути",
                missing="Нужны описание изменения и группы его выката",
                risk="Другие изменения могли повлиять на поведение покупателей",
                evidence_ids=[core["id"], release["id"]], contradictions=[], confidence="low",
            ))
    strengthen(hypotheses, evidence)
    for item in hypotheses:
        item["alternative_explanations"] = [other["cause"] for other in hypotheses if other is not item] or [
            "Проверить ошибку измерения, охват релиза и изменение состава аудитории."
        ]
        item["business_impact"] = (
            "Проверить влияние на самостоятельные заказы до оценки эффекта." if core
            else "Проверить влияние на выбранный продуктовый сценарий."
        )
    if hypotheses and persist:
        from .history import past_decisions, primary_signatures
        from .research import tokens
        past = [row for row in past_decisions(mode, query, limit=20, internal=True) if row["decision"] == "reject"]
        current = primary_signatures(evidence)
        def already_rejected(item):
            subject = tokens(item["cause"])
            used = {current[ref] for ref in item["evidence_ids"] if ref in current}
            for row in past:
                prior = tokens(row["hypothesis"])
                if prior and subject and len(subject & prior) / len(subject | prior) >= 0.7:
                    if used <= set(row["primary_signatures"]):
                        return True
            return False
        count = len(hypotheses)
        hypotheses = [item for item in hypotheses if not already_rejected(item)]
        if len(hypotheses) < count:
            limits.append("Сходное ранее отклонённое решение исключено: новых подтверждающих данных не найдено.")
    effect_plan = dict(
        primary_kpi="orders_index_4w",
        baseline=core["values"]["baseline_4w"] if core else None,
        target=None, target_basis="Согласовать цель после проверки разброса индекса в базовом периоде.",
        baseline_period=core["values"]["baseline_window"] if core else None,
        guardrails=["Контрольный ряд"] if control else [],
        control_baseline=control["values"]["baseline_4w"] if control else None,
        observation_weeks=4,
        stop_condition="Пересмотреть гипотезу при отсутствии улучшения или ухудшении контрольного показателя.",
    )
    proposals = [proposal | {"tag": ai_store.TASK_TAG} for proposal in task_proposals(
        hypotheses, matches, evidence, core, effect_plan, backlog_status=statuses["backlog"]["status"],
    )]
    elapsed = time.perf_counter() - started
    report = dict(
        run_id=uuid4().hex, llm_status=status, data_mode=mode, sources_status=statuses,
        evidence=evidence, summary=summary, findings=findings, hypotheses=hypotheses,
        recommended_actions=[h["verify"] for h in hypotheses] or ["Проверить полноту входных данных."],
        backlog_matches=matches, proposed_tasks=proposals, effect_plan=effect_plan, limitations=limits,
        trace=[dict(step="collect", seconds=round(gathered, 3)),
               *trace,
               dict(step="qwen", seconds=round(elapsed - gathered, 3), status=status,
                    reason=reason if status != "ok" else None,
                    selected_step=reason if status == "ok" else None)],
        usage=usage, termination=termination, seconds=round(elapsed, 3), checks=["checkout", "tracking"],
        hits=[item for item in evidence if item["kind"] in
              {"research", "interview", "competitor_local", "competitor_web"}],
        mode="Локальная Qwen" if status == "ok" else "Расчёт и поиск",
        model=os.getenv("PM_LLM_MODEL", "qwen3-4b") if status == "ok" else None,
        notice=("Модель не подключена или ответ не прошёл проверку. Показан расчёт и поиск."
                if status not in {"ok", "not_needed"} else ""),
    )
    report["request_context"] = {key: request[key] for key in ("query", "config", "metrika", "web", "task_id", "memory")
                                 if key in request}
    if previous:
        report["previous_run_id"] = previous["run_id"]
        old = {item["id"]: item for item in previous["evidence"]}
        new = {item["id"]: item for item in evidence}
        report["evidence_changes"] = {
            "added": sorted(new.keys() - old.keys()),
            "removed": sorted(old.keys() - new.keys()),
            "updated": sorted(ref for ref in new.keys() & old.keys() if any(
                new[ref].get(field) != old[ref].get(field) for field in ("values", "text", "quality", "period")
            )),
        }
    if status == "ok" and hypotheses:
        report.update(hypothesis=hypotheses[0]["cause"], proposal=hypotheses[0]["verify"],
                      refs=hypotheses[0]["evidence_ids"])
    if persist:
        ai_store.save_run(report)
    return report
