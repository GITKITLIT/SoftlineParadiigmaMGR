"""Exercise the archived evidence that AI Core gives to the Qwen chat."""

import argparse
from datetime import date, timedelta
import json
from math import isclose
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from llm_lab.evaluation.scenarios import scope_for, seed_history
from pm import ai_store, chat, investigation, memory, qwen


PRODUCT = "Demo A"
SPAN = ["2024-01-01", "2025-12-31"]
DECISION_REASON = "Показатель регистрации корзины ещё не восстановлен."


def seed():
    seed_history()
    memory.write_observations([{
        "mode": "synthetic", "stream": "release", "source": "qwen-eval",
        "product": PRODUCT, "metric": "release", "unit": "event", "aggregation": "snapshot",
        "method": "same-method", "timezone": "UTC", "start": day, "event_date": day,
        "subject": subject, "value": None, "quality": "complete",
    } for day, subject in [("2024-09-18", "DEMO-2024"), ("2025-04-16", "DEMO-2025")]])
    memory.write_observations([{
        "mode": "synthetic", "stream": "sales", "source": "qwen-eval",
        "product": PRODUCT, "metric": "orders", "unit": "orders", "aggregation": "sum",
        "method": "same-method", "timezone": "UTC",
        "start": (date(2025, 4, 21) + timedelta(days=offset)).isoformat(), "value": 17,
        "quality": "complete",
    } for offset in range(28)])
    with ai_store.database() as db:
        for day, value in [("2024-09-15", 100), ("2025-05-15", 125)]:
            memory.record_web(db, {
                "id": f"web:fixture-{day}", "kind": "competitor_web", "competitor": "one.example",
                "locator": "https://one.example/offer", "product": PRODUCT, "topic": "pricing",
                "data_mode": "synthetic", "quality": "page_excerpt",
                "observed_at": day + "T12:00:00Z", "retrieved_at": day + "T12:00:00Z",
                "offer": {"metric": "price", "value": value, "currency": "RUB", "terms": "annual demo"},
            })
    for index, text in enumerate((
        "В интервью обнаружена повторяющаяся проблема корзины при смене устройства.",
        "Другое интервью описало, как теряется выбранная опция корзины.",
    )):
        ai_store.store_excerpts(f"qwen-history-{index}", [{
            "id": f"fixture-{index}", "kind": "Интервью", "ref": f"Синтетическое интервью {index}",
            "product": PRODUCT, "text": text,
        }])
    with patch.dict(os.environ, {"PM_LLM_URL": ""}):
        previous = investigation.analyze({"query": "Почему выросло число проблем корзины?"})
    ai_store.decide({
        "run_id": previous["run_id"], "proposal_id": previous["proposed_tasks"][0]["id"],
        "action": "reject", "reason": DECISION_REASON, "idempotency_key": "historical-smoke",
    })
    with patch.dict(os.environ, {"PM_LLM_URL": "", "PM_SNAPSHOT": str(ROOT / "data/demo-overview.json")}):
        with patch.object(ai_store, "now", return_value="2025-03-04T00:00:00Z"):
            payment = investigation.analyze({"query": "Что произошло по оплате после изменения?"})
            task = ai_store.decide({
                "run_id": payment["run_id"], "proposal_id": payment["proposed_tasks"][0]["id"],
                "action": "accept", "target": 130, "idempotency_key": "historical-effect-fixture",
                "reason": "Проверить потери на этапе оплаты.",
            })
        fixed = date(2025, 4, 14)
        review = ai_store.review({
            "task_id": task["id"], "mode": "actual", "observation": {
                "fix": fixed.isoformat(), "rows": [
                    {"week": (fixed + timedelta(weeks=offset)).isoformat(),
                     "complete": True, "kaspersky": 80, "astra": 95}
                    for offset in range(4)
                ],
            },
        })
        assert review["status"] == "not_achieved"
    return {"rejected": previous["run_id"], "reviewed": payment["run_id"]}


def check_cases(*, live):
    stored = seed()
    series = scope_for(PRODUCT)["compare"]["series_id"]
    mode = {"mode": "synthetic"}
    def archive_scope(kind, args):
        return {"memory": mode | {kind: args}}

    cases = [
        ("year_ago", "Сравни продажи год назад и в последнем полном дне.", "compare_history",
         archive_scope("compare", {"series_id": series, "before": ["2024-12-31"] * 2, "after": ["2025-12-31"] * 2})),
        ("same_quarters", "Сравни одинаковые кварталы 2024 и 2025.", "compare_history",
         archive_scope("compare", {"series_id": series, "before": ["2024-10-01", "2024-12-31"],
                           "after": ["2025-10-01", "2025-12-31"]})),
        ("releases", "Сравни последствия релизов за два года.", "product_history",
         archive_scope("product", {"series_id": series, "product": PRODUCT, "period": SPAN})),
        ("offers", "Как менялись цены конкурентов?", "competitor_history",
         archive_scope("competitors", {"product": PRODUCT, "period": SPAN})),
        ("previous_problem", "Какие похожие проблемы корзины мы уже исследовали?", "search_history", {}),
        ("previous_decision", "Почему отклонили старую задачу корзины?", "search_history",
         archive_scope("search", {"product": "Лицензии A"})),
        ("other_product", "Какие похожие проблемы корзины мы уже исследовали?", "search_history",
         archive_scope("search", {"product": "Лицензии B"})),
        ("previous_effect", "Какой эффект у предыдущей рекомендации по оплате?", "search_history",
         {"memory": {"mode": "local", "search": {}}}),
        ("repeat_interviews", "Какие повторяющиеся проблемы корзины есть в интервью?", "search_history",
         {"memory": {"mode": "local", "search": {"product": PRODUCT}}}),
    ]
    checks = []
    for label, question, tool, scope in cases:
        report_view = chat.consult({"question": question, "scope": scope})
        report, reply = report_view["report"], report_view["reply"]
        assert report["llm_status"] == "not_needed", label
        step = next(row for row in report["trace"] if row.get("tool") == tool)
        if live:
            assert step["actor"] == "qwen", (label, step["actor"], report["termination"])
        assert all(row["tool"] in {"compare_history", "product_history", "search_history", "competitor_history"}
                   for row in report["trace"] if row["step"] == "tool"), label
        by_kind = {}
        for row in reply["sources"]:
            by_kind.setdefault(row["kind"], []).append(row)
        if label in {"year_ago", "same_quarters"}:
            result = by_kind["historical_comparison"][0]["values"]["delta_percent"]
            assert isclose(result, 100 * (14 / 11 - 1)), label
        elif label == "releases":
            assert {row["ref"]: row["values"]["delta"] for row in by_kind["historical_release"]} == {
                "DEMO-2024": 0, "DEMO-2025": 84,
            }
            assert all(row["values"]["interpretation"] == "descriptive_only" for row in by_kind["historical_release"])
        elif label == "offers":
            assert by_kind["historical_competitor"][0]["values"] == {"before": 100, "after": 125}
        elif label in {"previous_problem", "previous_decision"}:
            row = next(row for row in by_kind["memory_context"] if row["locator"]["run_id"] == stored["rejected"])
            assert row["values"]["reason"] == DECISION_REASON and "Отклонено" in reply["answer"]
        elif label == "previous_effect":
            row = next(row for row in by_kind["memory_context"] if row["locator"]["run_id"] == stored["reviewed"])
            assert row["values"]["effect"] == "not_achieved" and "Цель не достигнута" in reply["answer"]
        elif label == "other_product":
            assert not reply["sources"] and "подходящих наблюдений нет" in reply["answer"]
        else:
            assert len(by_kind["historical_document"]) == 2
            assert len({row["source_group"] for row in by_kind["historical_document"]}) == 2
        checks.append({"case": label, "tool": tool, "actor": step["actor"], "outcome": step["status"],
                       "llm_status": report["llm_status"]})
    return {"data": "synthetic", "checks": checks}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    with TemporaryDirectory(prefix="qwen-pim-smoke-") as state, patch.dict(os.environ, {
        "PM_PRIVATE_DIR": state, "PM_WORKBOOK": "", "PM_SNAPSHOT": "",
        "PM_CASE_CONFIG": "", "PM_EVIDENCE": "", "YANDEX_SEARCH_ENABLED": "false",
    } | ({} if args.live else {"PM_LLM_URL": ""})):
        if args.live and not qwen.endpoint():
            parser.error("Set PM_LLM_URL to the local llama-server")
        print(json.dumps(check_cases(live=args.live), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
