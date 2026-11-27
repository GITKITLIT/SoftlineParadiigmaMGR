"""Single, interval-gated capture for a local scheduler or analyst."""

import argparse
import html
import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pm.search_security import TOPIC_TERMS, domain_name, product_name, source_url
from pm.web_search import collect_competitor_evidence
from pm.yandex_search.comparison import build_comparison_context
from pm.yandex_search.config import enabled
from pm.yandex_search.parsing import utc_stamp
from pm.yandex_search.snapshots import History, compare_snapshots, history_scope, make_snapshot
from scripts.search_import_competitors import private_target, write_private_json, write_private_text
from scripts.search_smoke import OFFLINE_INPUT, offline_search


def read_json(path):
    with path.open("rb") as stream:
        payload = stream.read(32 * 1024 * 1024 + 1)
    if len(payload) > 32 * 1024 * 1024:
        raise ValueError("input_too_large")
    return json.loads(payload.decode("utf-8-sig"))


def public_plan(data):
    domains, topics = data["domains"], data["topics"]
    if type(domains) is not list or not 1 <= len(domains) <= 50:
        raise ValueError("invalid_domain_list")
    if type(topics) is not list or not 1 <= len(topics) <= len(TOPIC_TERMS) or not all(
        isinstance(value, str) and value in TOPIC_TERMS for value in topics
    ):
        raise ValueError("invalid_topic_list")
    return {
        "product": product_name(data["product"]),
        "domains": list(dict.fromkeys(domain_name(value) for value in domains)),
        "topics": list(dict.fromkeys(topics)),
    }


def validate_result(result, plan, mode):
    sources = {"yandex_search"} if mode == "live" else {"yandex_search", "synthetic_fixture"}
    if (
        result.get("schema_version") != 1 or result.get("source") not in sources
        or result.get("status") not in {"ok", "partial", "disabled", "unavailable", "error"}
        or type(result.get("items")) is not list or len(result["items"]) > 5010
        or type(result.get("warnings")) is not list
        or type(result.get("usage", {}).get("requests")) is not int
        or not 0 <= result["usage"]["requests"] <= 50
    ):
        raise ValueError("invalid_v1_result")
    snippet_ids = {item.get("id") for item in result["items"] if item.get("content_type") == "snippet"
                   and item.get("domain") in plan["domains"]}
    for item in result["items"]:
        domain = domain_name(item.get("domain"))
        valid_domain = domain in plan["domains"]
        if not valid_domain and item.get("content_type") == "page_excerpt":
            valid_domain = bool(snippet_ids.intersection(item.get("provenance", {}).get("search_evidence_ids", ())))
        if (
            item.get("kind") != "competitor_web" or item.get("product") != plan["product"]
            or item.get("content_type") not in {"snippet", "page_excerpt"}
            or item.get("topic") not in plan["topics"]
            or not valid_domain or not source_url(item.get("url"), domain)
            or not all(isinstance(item.get(key), str) for key in ("id", "ref", "text", "retrieved_at"))
        ):
            raise ValueError("evidence_outside_plan")


def capture(*, history, plan, mode, provider, local, own=None, aliases=None, interval=0,
            as_of=None, now=None, price_threshold_pct=5):
    now = now or datetime.now(UTC)
    scope = history_scope(plan, mode)
    token, reason = history.claim(scope, now, interval)
    if token is None:
        return {"status": "skipped", "reason": reason, "mode": mode}
    try:
        result = provider()
        validate_result(result, plan, mode)
        context = build_comparison_context(
            web_result=result, local_evidence=local, own_evidence=own, as_of=as_of,
            competitor_aliases=aliases,
        )
        snapshot = make_snapshot(context, result, scope=scope, now=now, mode=mode)
        snapshot["plan"] = plan
        previous = history.latest(scope, usable=True)
        changes = compare_snapshots(previous, snapshot, price_threshold_pct=price_threshold_pct)
        confirmed = any(cell["status"] == "confirmed" for row in snapshot["matrix"]["rows"]
                        for cell in row["competitors"].values())
        usable = confirmed and (result["status"] == "ok" or mode == "offline")
        snapshot["evidence_source"] = result["source"]
        snapshot["changes_from_previous"] = changes
        history.finish(scope, token, snapshot, usable=usable)
    except BaseException:
        try:
            history.finish(scope, token)
        except (ValueError, sqlite3.Error):
            pass
        raise
    return {"status": "captured", "mode": mode, "snapshot": snapshot, "changes": changes}


def empty_result():
    return {
        "schema_version": 1, "source": "yandex_search", "status": "disabled", "reason": "local_only",
        "retrieved_at": utc_stamp(datetime.now(UTC)), "items": [], "warnings": [],
        "usage": {"requests": 0, "cache_hits": 0},
    }


def _cell(value):
    text = " ".join(str(value if value is not None else "неизвестно").split())
    return html.escape(text)[:300].replace("|", "&#124;").replace("[", "&#91;").replace("]", "&#93;").replace(
        "`", "&#96;"
    )


def markdown(report):
    snapshot = report["snapshot"]
    changes = report["changes"]
    lines = [
        "# Конкурентное исследование", "",
        f"Продукт {_cell(snapshot['plan']['product'])}. Снимок {_cell(snapshot['captured_at'])}.",
        f"Режим {_cell(snapshot['mode'])}. Статус поиска {_cell(snapshot['search']['status'])}.",
        f"Существенных изменений {changes['material_changes']}.", "",
        "| Характеристика | Конкурент | Наблюдение | Дата | Сопоставимость | Источники |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    index = snapshot["evidence_index"]
    for row in snapshot["matrix"]["rows"]:
        for name, value in row["competitors"].items():
            refs = ", ".join(index[key]["ref"] for key in value["evidence_ids"] if key in index)
            entries = (row["label"], name, value["value"], value["as_of"], value["comparison"]["status"], refs)
            lines.append("| " + " | ".join(map(_cell, entries)) + " |")
    if changes["changes"]:
        lines.extend(["", "## Изменения", ""])
        for change in changes["changes"]:
            lines.append(f"- {_cell(change['competitor'])} · {_cell(change['feature'])} · "
                         f"{_cell(change['reason'])} · {_cell(change['before']['value'])} → "
                         f"{_cell(change['after']['value'])}")
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description="Локальный снимок и сравнение конкурентных исследований")
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--demo", action="store_true", help="Синтетический поиск без сети")
    modes.add_argument("--offline", action="store_true", help="Локальный корпус и сохранённый ответ v1")
    modes.add_argument("--live", action="store_true", help="Поиск с согласованным бюджетом")
    parser.add_argument("--history-dir", required=True, type=Path)
    parser.add_argument("--plan", type=Path, help="JSON с product, domains и topics")
    parser.add_argument("--web-result", type=Path, help="Сохранённый v1 JSON для --offline")
    parser.add_argument("--local-corpus", type=Path)
    parser.add_argument("--own-offer", type=Path)
    parser.add_argument("--competitor-aliases", type=Path)
    parser.add_argument("--as-of", help="Дата анализа для офлайн-сценария")
    parser.add_argument("--minimum-interval-minutes", type=int)
    parser.add_argument("--price-threshold-pct", type=float, default=5)
    parser.add_argument("--report", type=Path, help="Новый файл .json или .md вне Git")
    args = parser.parse_args()
    if (not args.demo and not args.plan) or (args.live and args.as_of) or (args.web_result and not args.offline):
        parser.error("Проверьте --plan, --as-of и --web-result для выбранного режима.")
    interval = args.minimum_interval_minutes
    interval = (1440 if args.live else 0) if interval is None else interval
    if not (15 if args.live else 0) <= interval <= 525600 or not 0 < args.price_threshold_pct <= 100:
        parser.error("Укажите допустимый интервал и порог изменения цены.")
    try:
        if args.report:
            suffix = args.report.suffix.lower()
            if suffix not in {".md", ".json"}:
                raise ValueError("invalid_report_format")
            private_target(args.report, suffix)
        if args.live and not (enabled("YANDEX_SEARCH_MONITOR_ENABLED") and enabled("YANDEX_SEARCH_ENABLED")):
            parser.error("Живой мониторинг требует оба флага ENABLED и согласованный бюджет.")
        plan = public_plan(OFFLINE_INPUT if args.demo else read_json(args.plan))
        local = read_json(args.local_corpus) if args.local_corpus else []
        own = read_json(args.own_offer) if args.own_offer else None
        aliases = read_json(args.competitor_aliases) if args.competitor_aliases else None
        offline = read_json(args.web_result) if args.web_result else empty_result()
        mode = "live" if args.live else "demo" if args.demo else "offline"
        if args.live:
            provider = lambda: collect_competitor_evidence(**plan)
        elif args.demo:
            provider = offline_search
        else:
            provider = lambda: offline
        report = capture(
            history=History(args.history_dir), plan=plan, mode=mode, provider=provider, local=local,
            own=own, aliases=aliases, interval=interval * 60, as_of=args.as_of,
            price_threshold_pct=args.price_threshold_pct,
        )
        if args.report and report["status"] == "captured":
            if suffix == ".json":
                write_private_json(args.report, report)
            else:
                write_private_text(args.report, markdown(report), suffix=".md")
    except (OSError, ValueError, TypeError, KeyError, AttributeError, sqlite3.Error, RecursionError) as error:
        parser.exit(2, f"Снимок недоступен ({type(error).__name__}). Проверьте пути, JSON и конфигурацию.\n")
    if report["status"] == "skipped":
        print(json.dumps(report))
        return 0
    print(json.dumps({
        "mode": mode, "status": report["snapshot"]["search"]["status"],
        "snapshot_id": report["snapshot"]["id"], "material_changes": report["changes"]["material_changes"],
        "cost": report["snapshot"]["cost"],
    }))
    return int(report["snapshot"]["search"]["status"] in {"error", "unavailable"}
               or (args.live and report["snapshot"]["search"]["status"] == "disabled"))


if __name__ == "__main__":
    raise SystemExit(main())
