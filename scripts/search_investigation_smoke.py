"""Run a synthetic investigation through the local evidence lookup and search v1."""

import json
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from openpyxl import Workbook

from pm import research
from pm.competitor_sources import load_competitor_evidence
from pm.service import overview
from pm.yandex_search.comparison import build_comparison_context
from scripts.search_smoke import offline_search


def run():
    with TemporaryDirectory() as directory:
        private = Path(directory)
        workbook = private / "synthetic-competitors.xlsx"
        corpus_file = private / "synthetic-evidence.json"
        book = Workbook()
        book.active.append(["Товар", "Конкурент", "Дата исследования", "Путь покупки"])
        book.active.append(["Демо Pro", "Демо-магазин", "2026-11-03",
                            "Демо: сначала выбирают лицензию, затем переходят в корзину."])
        book.save(workbook)
        book.close()
        local = load_competitor_evidence(workbook)
        web = offline_search()
        demo = json.loads((ROOT / "data/demo-evidence.json").read_text(encoding="utf-8"))
        records = demo + local + web["items"]
        corpus_file.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
        query = "Как проверить добавление лицензии в корзину?"
        # The search provider gets only the fixed OFFLINE_INPUT from the smoke fixture.
        with patch.dict(os.environ, {
            "PM_PRIVATE_DIR": directory, "PM_EVIDENCE": str(corpus_file),
            "YANDEX_SEARCH_ENABLED": "false",
        }, clear=True):
            snapshot = overview()
            analysis = research.analyze(query, snapshot)
        context = build_comparison_context(web_result=web, local_evidence=local)
        expected_ids = {"sales", "funnel", "backlog"} | {item["id"] for item in local + web["items"]}
        found_ids = {item["id"] for item in analysis["hits"]}
        scopes = {item["source"]["scope"] for item in context["observations"]}
        if not expected_ids <= found_ids or scopes != {"local_study", "search_snippet"}:
            raise RuntimeError("Не пройдена проверка связей между синтетическими источниками")
        return {
            "mode": "offline",
            "evidence_ids": sorted(found_ids),
            "source_scopes": sorted(scopes),
            "backlog_id": snapshot["backlog"]["id"],
            "checks": analysis["checks"],
            "web_usage": web["usage"],
        }


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=True, indent=2))
