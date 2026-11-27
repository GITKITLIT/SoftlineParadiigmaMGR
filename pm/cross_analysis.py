"""Build reproducible cross-source findings and qualify independent evidence."""
from datetime import timedelta

from .analytics import change, day
from .research import tokens

PAIRINGS = (
    ("metrika", "interview", "metrics_and_research",
     "Сверить наблюдения пользователей с зарегистрированным шагом по одному сегменту.",
     "Нужна карта соответствия шага, цели, сегмента и периода."),
    ("interview", "competitor_local", "research_and_competitors",
     "Сопоставить описанный пользователями барьер с тем же шагом в конкурентном сценарии.",
     "Нужны одинаковые условия покупки, сопоставимый сегмент и дата наблюдения."),
    ("interview", "competitor_web", "research_and_public_competitors",
     "Проверить публичное описание того же шага, на котором пользователи отмечают барьер.",
     "Сниппет или описание страницы требует проверки на практике."),
    ("competitor_local", "backlog", "competitors_and_backlog",
     "Сверить, покрывает ли существующая задача обнаруженное конкурентное различие.",
     "Нужны критерии приёмки существующей задачи и свежая проверка конкурента."),
    ("competitor_web", "backlog", "public_competitors_and_backlog",
     "Сверить публичное описание функции с объёмом существующей задачи.",
     "Публичный фрагмент не подтверждает качество поведения продукта."),
)


def finding(code, text, ids, *, contrary=(), missing=(), checks=(), statement="fact", confidence="medium", values=None):
    result = dict(relation=code, statement_type=statement, text=text, evidence_ids=list(ids),
                  contradictions=list(contrary), missing=list(missing), checks=list(checks),
                  confidence=confidence)
    if values is not None:
        result["values"] = values
    return result


def correlate(evidence):
    by_id = {item["id"]: item for item in evidence}
    by_kind = {}
    for item in evidence:
        by_kind.setdefault(item["kind"], []).append(item)
    results = []
    primary, control = by_id.get("orders:0"), by_id.get("orders:1")
    if primary and control and (
        primary["period"], primary["metric"], primary["filter"]
    ) == (control["period"], control["metric"], control["filter"]):
        first, second = primary["values"]["delta"], control["values"]["delta"]
        if first is not None and second is not None:
            gap = first - second
            text = f"Разница изменений основной и контрольной групп {gap:+.1f} п. п. Сравнение описательное."
            contrary = [control["id"]] if first * second < 0 or (first < 0 and second == 0) else []
            results.append(finding("sales_control", text, [primary["id"], control["id"]],
                                   contrary=contrary, checks=["Проверить сопоставимость групп до изменения."],
                                   values={"difference_pp": gap}))
    if primary:
        for metric in [x for x in by_kind.get("metrika", []) if x["id"].startswith("metrika:before:")]:
            name = metric["id"].split(":", 2)[-1]
            later = by_id.get(f"metrika:after:{name}")
            if not later or metric["data_mode"] != "store_counter" or later["data_mode"] != "store_counter":
                continue
            if metric["filter"] != later["filter"] or metric["quality"] != "observed" or later["quality"] != "observed":
                continue
            # Ratios and durations are not volumes; use an explicit configured goal or a count.
            if metric["unit"] not in {"visits", "users", "views", "completions"}:
                continue
            metric_delta = change(metric["values"]["value"], later["values"]["value"])
            sales_delta = primary["values"]["delta"]
            if metric_delta is None or sales_delta is None:
                continue
            def same_window(item, key):
                dates = primary["period"][key]
                period = item.get("period") or {}
                return (period.get("start") == dates[0] and day(period["end"]) == day(dates[1]) + timedelta(days=6)
                        if period.get("end") else False)
            if not same_window(metric, "before") or not same_window(later, "after"):
                continue
            opposite = (sales_delta < 0 <= metric_delta) or (sales_delta > 0 >= metric_delta)
            ids = [primary["id"], metric["id"], later["id"]]
            results.append(finding(
                "sales_metrika", f"В совпадающих окнах индекс заказов изменился на {sales_delta:+.1f}%, "
                f"показатель {name} — на {metric_delta:+.1f}%.",
                ids, contrary=ids[1:] if opposite else [],
                checks=["Сверить охват счётчика и продукта до вывода о конверсии."],
                missing=["Совпадение товарной группы и сегмента не подтверждено."],
                values={"orders_delta": sales_delta, "metrika_delta": metric_delta},
            ))
            if len([row for row in results if row["relation"] == "sales_metrika"]) >= 2:
                break
        period = primary.get("period") or {}
        for release in by_kind.get("release", [])[:3]:
            when = release.get("observed_at")
            if when and period.get("before") and day(period["before"][0]) <= day(when) <= day(period["after"][1]) + timedelta(days=6):
                results.append(finding(
                    "sales_release", "Изменение индекса зафиксировано в окне с релизом.",
                    [primary["id"], release["id"]],
                    missing=["Неизвестны охват релиза и влияние других изменений."],
                    checks=["Сопоставить дату воздействия и затронутый сегмент с динамикой."],
                    statement="assumption", confidence="low",
                ))
    for left, right, code, description, missing in PAIRINGS:
        # Require matching vocabulary, not merely two source types in the run.
        for a in by_kind.get(left, []):
            text_terms = tokens(a["text"] + " " + a["ref"])
            candidates = [b for b in by_kind.get(right, [])
                          if text_terms & tokens(b["text"] + " " + b["ref"])
                          and a.get("source_group") != b.get("source_group")]
            if not candidates:
                continue
            b = candidates[0]
            results.append(finding(code, description, [a["id"], b["id"]],
                                   missing=[missing], checks=[description], statement="assumption", confidence="low"))
            break
    return results


def strengthen(hypotheses, evidence):
    by_id = {item["id"]: item for item in evidence}
    for hypothesis in hypotheses:
        confirmed = [by_id[ref] for ref in hypothesis["evidence_ids"] if ref in by_id]
        groups = {item.get("source_group") or item["id"] for item in confirmed
                  if item["quality"] not in {"incomplete", "snippet", "sampled", "stale", "derived", "historical_context", "reported"}
                  and item["kind"] != "memory_context"
                  and item.get("freshness") not in {"stale", "future"}
                  and not (item["kind"] in {"competitor_local", "competitor_web"}
                           and item.get("freshness") == "unknown")
                  and item["data_mode"] != "demo_counter"}
        hypothesis["statement_type"] = "hypothesis"
        hypothesis["independent_sources"] = len(groups)
        hypothesis["confidence"] = "medium" if len(groups) > 1 and not hypothesis["contradictions"] else "low"
