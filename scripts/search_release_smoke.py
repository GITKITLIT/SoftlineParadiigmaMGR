"""Rehearse v1, PIM and Qwen handoff in an isolated AI Core checkout, offline."""

import base64
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from openpyxl import Workbook

from pm import ai_store, analysis_context, history, memory, qwen
from pm.competitor_sources import load_competitor_evidence
from pm.yandex_search.pim import history_batch, project_snapshot, write_pim_snapshot
from pm.yandex_search.qwen_handoff import pack_competitor_context
from scripts.search_monitor import capture, empty_result
from pm.yandex_search.snapshots import History


def require(condition, context):
    if not condition:
        raise RuntimeError(context)


def run():
    product = "Kaspersky Endpoint Security"
    domain = "example.com"
    plan = {"product": product, "domains": [domain], "topics": ["pricing"]}
    with TemporaryDirectory() as private:
        directory = Path(private)
        corpus = directory / "synthetic.xlsx"
        book = Workbook()
        sheet = book.active
        sheet.title = "Синтетика"
        sheet.append(["Продукт", "Конкурент", "Дата исследования", "Версия", "Тип лицензии", "Срок",
                      "Устройства", "НДС", "Валюта", "Цена", "Самостоятельная покупка"])
        for date, price, self_checkout in (("2025-03-19", 1000, "Да"), ("2026-11-25", 1120, "Нет")):
            sheet.append([product, domain, date, "2", "Стандарт", "12 месяцев", 1, "включён", "RUB",
                          price, self_checkout])
        book.create_sheet("Эталонный_кейс").append(["EXCLUDED SYNTHETIC REFERENCE"])
        book.save(corpus)
        book.close()
        records = load_competitor_evidence(corpus)
        require(len(records) == 2 and records[0]["locator"]["cell"] == "A2:K2", "workbook")
        env = {
            "PM_PRIVATE_DIR": private, "PM_EVIDENCE": str(directory / "evidence.json"),
            "YANDEX_SEARCH_ENABLED": "true", "YANDEX_SEARCH_PUBLIC_PRODUCTS": json.dumps([product]),
            "YANDEX_SEARCH_ALLOWED_DOMAINS": json.dumps([domain]), "YANDEX_SEARCH_API_KEY": "fixture-key",
            "YANDEX_SEARCH_FOLDER_ID": "fixture-folder", "YANDEX_SEARCH_PAGE_FETCH_ENABLED": "true",
            "YANDEX_SEARCH_PAGE_ALLOWED_DOMAINS": json.dumps([domain]), "PYTHONUTF8": "1",
        }
        Path(env["PM_EVIDENCE"]).write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
        html = (ROOT / "tests/fixtures/yandex/competitor-page.html").read_text(encoding="utf-8")
        html = html.replace("Демо Pro", product)
        xml = f"""<yandexsearch><response><results><grouping><group><doc>
          <url>https://{domain}/demo-product</url><title>{product}</title>
          <passages><passage>Синтетическая цена и лицензия {product}</passage></passages>
          </doc></group></grouping></results></response></yandexsearch>""".encode()
        result = json.dumps({"rawData": base64.b64encode(xml).decode()}).encode()
        with patch.dict(os.environ, env, clear=True), patch(
            "pm.web_search.request_once", return_value=result,
        ) as provider, patch(
            "pm.yandex_search.pages.resolve_public", return_value="fixture",
        ), patch("pm.yandex_search.pages._request", side_effect=[(404, None, None), (200, None, html)]), patch(
            "pm.yandex_search.pages.time.sleep",
        ):
            status, _, evidence = analysis_context.public_search(plan)
            require(status == "ok" and len(evidence) == 2 and provider.call_count == 1, "v1 normalizer")
            page, snippet = evidence
            require(page.get("offer", {}).get("currency") == "RUB", "AI Core verified offer")
            require(snippet["quality"] == "snippet" and "offer" not in snippet, "snippet restrictions")
            with ai_store.database() as db:
                memory.record_web(db, page | {"data_mode": "synthetic"})
                memory.record_web(db, snippet | {"data_mode": "synthetic"})

            snapshots = History(directory)
            for source, stamp in zip(records, (
                datetime(2025, 3, 19, 8, tzinfo=UTC), datetime(2026, 11, 25, 8, tzinfo=UTC),
            ), strict=True):
                report = capture(history=snapshots, plan=plan, mode="offline", provider=empty_result,
                                 local=[source], now=stamp, as_of=stamp.date())
            first, second = history_batch(directory)
            def transfer(batch):
                return write_pim_snapshot(batch["snapshot"], batch["previous"], mode="synthetic")
            old = transfer(first)
            with ThreadPoolExecutor(max_workers=2) as pool:
                parallel = list(pool.map(transfer, [second, second]))
            require(parallel[0]["ids"] == parallel[1]["ids"], "concurrent PIM replay")
            replay = transfer(first)
            require(replay["created"] == 0 and replay["ids"] == old["ids"], "historical replay")
            price_row = next(row for row in project_snapshot(second["snapshot"], second["previous"],
                                                             mode="synthetic") if row["metric"] == "price")
            require(price_row["metadata"]["previous_snapshot_id"] == first["snapshot"]["id"], "PIM lineage")
            period = ["2025-03-01", "2026-11-27"]
            timeline = history.competitor_changes({"mode": "synthetic", "period": period, "product": product})
            require(any(x["before"] == 1000 and x["after"] == 1120 for x in timeline), "PIM history query")
            require(not history.competitor_changes({"mode": "local", "period": period, "product": product}),
                    "PIM partition")
            packed = pack_competitor_context(report["snapshot"], changes=report["changes"])
            require(packed["changes"] and all(row["evidence_ids"] for row in packed["changes"]), "Qwen history")
            local_model_input = qwen.bounded_evidence(evidence, 3200, 240)
            require(len(local_model_input) == 2 and "<script" not in json.dumps(local_model_input), "Qwen pack")
            return {
                "mode": "synthetic_offline", "v1_status": status, "evidence_types": [x["quality"] for x in evidence],
                "pim_inserted": old["created"] + sum(row["created"] for row in parallel),
                "replayed_insertions": replay["created"], "parallel_pim_replay": "same_ids",
                "pim_historical_price_changes": len(timeline), "qwen_historical_claims": len(packed["changes"]),
                "qwen_handoff_bytes": len(json.dumps(packed, ensure_ascii=False, separators=(",", ":")).encode()),
                "api_fixture_calls": provider.call_count, "live_requests": 0, "qwen_model_calls": 0,
            }


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
