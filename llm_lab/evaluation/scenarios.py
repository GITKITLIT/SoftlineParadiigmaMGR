"""Synthetics independent from R-09 and model prompts."""

from copy import deepcopy
from datetime import date, timedelta
from math import isclose

from pm import ai_store, history, investigation, memory
from pm.analysis_context import TOOLS

WEB_SCOPE = {
    "product": "Demo A", "domains": ["one.example", "two.example"],
    "topics": ["pricing", "licensing"],
}
WEB_ENV = {
    "YANDEX_SEARCH_ENABLED": "true", "YANDEX_SEARCH_ALLOWED_DOMAINS": '["one.example","two.example"]',
    "YANDEX_SEARCH_PUBLIC_PRODUCTS": '["Demo A"]',
}
CASES = {
    "drop": "Почему упали заказы? Сопоставь продажи, контроль, воронку, релиз, интервью, конкурентов и бэклог.",
    "no_anomaly": "Есть ли снижение заказов?",
    "nearby_releases": "Какие релизы совпали со снижением заказов?",
    "tracking_gap": "Проверь воронку и регистрацию события корзины. Что можно проверить первым?",
    "missing_weeks": "Есть ли снижение заказов при пропусках?",
    "unequal_periods": "Оцени масштаб изменения заказов при разных окнах.",
    "no_control": "Почему сократились заказы? Сопоставь контроль.",
    "contradiction": "Проверь заказы и визиты в Метрике за те же окна.",
    "stale_research": "Что известно из локального конкурентного исследования о покупке лицензии?",
    "incomparable_offers": "Какие публичные доказательства о ценах и лицензиях конкурентов сопоставимы?",
    "search_unavailable": "Доступны ли публичные конкурентные доказательства о подписке?",
    "metrika_unavailable": "Доступны ли визиты в Метрике?",
    "model_unavailable": "Почему изменились заказы?",
    "simple_chat": "Какие задачи есть в бэклоге?",
    "follow_up": "А что ей противоречит?",
    "product_switch": "Сравни исторические периоды для Demo B.",
    "history": "Сравни исторические периоды Demo A за два года.",
    "duplicate": "Какие задачи в бэклоге уже проверяют регистрацию событий в корзине?",
    "failed_effect": "Какой эффект показала согласованная задача?",
    "out_of_scope": "Как испечь хлеб?",
    "draft": "Какую гипотезу и задачу проверки предложить при снижении заказов?",
    "monitor": "Автоматическое расследование по сигналу.",
    "release_brief": "Что произошло после релиза и что проверить первым?",
}
STUB_SEARCH = {"search_unavailable", "incomparable_offers"}
CHAT = {"simple_chat", "follow_up", "product_switch", "history", "draft", "failed_effect", "out_of_scope"}


def seed_history():
    points = []
    for offset in range(731):
        day = date(2024, 1, 1) + timedelta(days=offset)
        for product, amount in (("Demo A", 14 if day.year == 2025 else 11),
                                ("Demo B", 13 if day.year == 2025 else 16)):
            points.append({
                "mode": "synthetic", "stream": "sales", "source": "qwen-eval", "product": product,
                "metric": "orders", "unit": "orders", "aggregation": "sum", "method": "same-method",
                "timezone": "UTC", "start": day.isoformat(), "value": amount, "quality": "complete",
            })
    memory.write_observations(points)


def scope_for(product):
    series = next(row for row in history.series_index("synthetic", product=product)
                  if row["metric"] == "orders" and row["stream"] == "sales")
    return {"mode": "synthetic", "compare": {"series_id": series["id"],
            "before": ["2024-02-01", "2024-02-28"], "after": ["2025-02-01", "2025-02-28"]}}


def fixture_snapshot(case, original):
    snapshot = deepcopy(original)
    primary = snapshot["series"][0]
    if case == "no_anomaly":
        primary["points"] = [[day, 100, 100] for day, _, _ in primary["points"]]
    if case == "missing_weeks":
        primary["points"].pop(3)
    if case == "no_control":
        snapshot["series"] = [primary]
    if case == "nearby_releases":
        snapshot["release_history"] = [{"id": "DEMO-NEAR", "date": "2025-03-17",
                                        "title": "Изменение каталога", "related_task": "DEMO-TASK"}]
    if case == "duplicate":
        snapshot["backlog_history"] = [{"id": "T-DEMO-DUP", "title": "Проверить регистрацию событий в корзине",
                                        "status": "В работе"}]
    return snapshot


def meter_for(case):
    def meter(start, end, *, segment, detailed):
        if case == "metrika_unavailable":
            raise OSError("Fixture has no connection")
        visits = 1200 if start >= "2025-03-10" else 1000
        return {"status": "ok", "scope": "store_counter", "segment": segment,
                "period": {"start": start, "end": end},
                "metrics": {"visits": {"value": visits, "metric": "fixture:visits", "unit": "visits"}}}
    return meter


def search_fixture(case):
    def collect(*, product, domains, topics):
        if case == "search_unavailable":
            return {"schema_version": 1, "status": "unavailable", "reason": "fixture_timeout", "items": []}
        items = []
        for domain, content_type, currency, period in (
            ("one.example", "page_excerpt", "RUB", "год"),
            ("two.example", "snippet", "USD", "месяц"),
        ):
            items.append({
                "kind": "competitor_web", "id": f"web:{domain}", "title": f"Условия подписки {domain}",
                "text": "На странице описана годовая лицензия." if content_type == "page_excerpt" else "В сниппете упомянута оплата за месяц.", "url": f"https://{domain}/offer",
                "domain": domain, "product": product, "topic": "pricing", "content_type": content_type,
                "retrieved_at": "2025-01-03T00:00:00Z", "published_at": "2024-12-30T00:00:00Z",
                "attributes": {"product": product, "version": "Pro", "license_type": "подписка",
                               "tax": "включён", "duration": period, "devices": "3",
                               "price": "100", "currency": currency},
            })
        return {"schema_version": 1, "status": "ok", "source": "synthetic_fixture", "items": items}
    return collect


def effect_task():
    def infer(q, evidence):
        return "ok", [{
            "problem": "Диагностика покупки", "cause": "Нарушен путь покупки",
            "verify": "Воспроизвести путь покупки", "expected": "Шаг покупки будет доступен",
            "missing": "Нужно проверить контроль", "risk": "Сезонность", "confidence": "low",
            "evidence_ids": ["orders:0"], "contradictions": [],
        }], {}, None
    report = investigation.analyze({"query": "Проверь ордера"}, infer=infer)
    task = ai_store.decide({"run_id": report["run_id"], "proposal_id": "proposal-1",
                            "action": "accept", "target": 125, "idempotency_key": report["run_id"]})
    observation = {"fix": "2025-04-14", "rows": [
        {"week": (date(2025, 4, 14) + timedelta(weeks=i)).isoformat(),
         "kaspersky": 80, "astra": 95, "complete": True} for i in range(4)]}
    review = ai_store.review({"task_id": task["id"], "mode": "simulation", "observation": observation})
    assert review["status"] == "not_achieved"
    return task["id"]


def checks(case, report):
    evidence = {item["id"]: item for item in report["evidence"]}
    assertions = {
        "citations_resolve": all(set(row["evidence_ids"] + row["contradictions"]) <= evidence.keys()
                                for row in report["hypotheses"] + report["findings"]),
        "hypotheses_testable": all(row["verify"].strip() and row["expected"].strip() and row["risk"].strip()
                                   for row in report["hypotheses"]),
        "only_allowlisted_sources": all(event["tool"] in TOOLS
                                        for event in report["trace"] if event["step"] == "tool"),
    }
    primary = evidence.get("orders:0", {}).get("values", {})
    delta = primary.get("delta")
    statuses = {key: value["status"] for key, value in report["sources_status"].items()}
    if case in {"drop", "draft", "monitor", "release_brief"}:
        assertions["correct_decline"] = delta is not None and isclose(delta, -30)
        assertions["emits_verifiable_hypotheses"] = bool(report["hypotheses"])
    if case == "no_anomaly":
        assertions["no_false_decline"] = delta == 0 and not report["hypotheses"]
    if case == "nearby_releases":
        assertions["additional_release"] = "release:nearby:1" in evidence
        assertions["causality_qualified"] = all(row["statement_type"] != "fact"
                                                for row in report["findings"] if row.get("relation") == "sales_release")
    if case == "tracking_gap":
        assertions["reach_is_not_checkout"] = (statuses["funnel"] == "partial"
                                                and all(row.get("metric") != "add_to_cart" for row in evidence.values()))
    if case == "missing_weeks":
        assertions["missing_delta"] = statuses["sales"] == "partial" and delta is None
    if case == "unequal_periods":
        assertions["different_windows_disclosed"] = any("Длина окон" in value for value in report["limitations"])
    if case == "no_control":
        assertions["no_control"] = "orders:1" not in evidence and any("Контрольная группа" in v for v in report["limitations"])
    if case == "contradiction":
        assertions["retains_contradictory_sources"] = any(row.get("relation") == "sales_metrika" and row["contradictions"]
                                                          for row in report["findings"])
    if case == "stale_research":
        assertions["qualified_source_age"] = any(row["kind"] == "competitor_local" and row["freshness"] == "stale"
                                                  for row in evidence.values())
    if case == "incomparable_offers":
        assertions["unverified_snippet_does_not_become_offer"] = (
            evidence.get("web:two.example", {}).get("quality") == "snippet"
            and "offer" not in evidence["web:two.example"] and "offer" in evidence.get("web:one.example", {})
        )
    if case == "search_unavailable":
        assertions["provider_failure_disclosed"] = statuses["competitor_web"] == "unavailable"
    if case == "metrika_unavailable":
        assertions["counter_failure_disclosed"] = statuses["metrika"] == "unavailable"
    if case == "model_unavailable":
        assertions["explicit_fallback"] = report["llm_status"] == "unavailable" and bool(report["notice"])
    if case == "simple_chat":
        assertions["observed_backlog"] = "backlog:related" in evidence
    if case in {"history", "product_switch"}:
        expected = 100 * (14 / 11 - 1) if case == "history" else 100 * (13 / 16 - 1)
        result = evidence.get("memory:comparison", {}).get("values", {}).get("delta_percent")
        assertions["correct_history"] = result is not None and isclose(result, expected)
    if case == "duplicate":
        assertions["related_task_retrieved"] = any(row["id"] == "T-DEMO-DUP" for row in report["backlog_matches"])
    if case == "failed_effect":
        # A saved simulation must not be presented as the observed effect.
        assertions["simulation_not_observation"] = evidence.get("task:effect", {}).get("values", {}).get("status") == "waiting_for_data"
    if case == "out_of_scope":
        assertions["no_data_lookups"] = not evidence and report["termination"] == "out_of_scope"
    return {key: bool(value) for key, value in assertions.items()}
