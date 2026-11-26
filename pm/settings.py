"""Choose the demo case or load a local case configuration."""

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

DEMO_CASE = {
    "release_id": "DEMO-01",
    "backlog_id": "TASK-02",
    "families": ["Лицензии A", "Лицензии B"],
    "channel": "Самостоятельные заказы клиентов",
    "reach_product": "Демо-конфигуратор",
    "periods": {
        "before_start": "2025-02-03",
        "before_end": "2025-02-24",
        "after_start": "2025-03-10",
        "after_end": "2025-03-31",
        "partial_week": "2025-04-07",
        "reach_start": "2025-03-10",
        "reach_end": "2025-04-06",
    },
}


def workbook_case():
    path = os.getenv("PM_CASE_CONFIG")
    if not path:
        return DEMO_CASE
    config = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    return DEMO_CASE | config


def private_dir():
    path = os.getenv("PM_PRIVATE_DIR")
    return Path(path).expanduser() if path else Path.home() / ".paradiigmamgr"
