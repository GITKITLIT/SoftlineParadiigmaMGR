"""Exercise v1 with a synthetic response, or send one explicitly enabled request."""

import argparse
import base64
import json
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pm.search_security import TOPIC_TERMS
from pm.web_search import _collect, collect_competitor_evidence
from scripts.search_import_competitors import write_private_json

OFFLINE_INPUT = {"product": "Демо Pro", "domains": ["example.com"], "topics": ["licensing"]}


def offline_search():
    raw = (ROOT / "tests/fixtures/yandex/offline.xml").read_bytes()
    payload = json.dumps({"rawData": base64.b64encode(raw).decode("ascii")}).encode("utf-8")
    with TemporaryDirectory() as private:
        env = {
            "PM_PRIVATE_DIR": private,
            "YANDEX_SEARCH_ENABLED": "true",
            "YANDEX_SEARCH_FOLDER_ID": "demo-folder",
            "YANDEX_SEARCH_API_KEY": "synthetic-placeholder-key",
            "YANDEX_SEARCH_ALLOWED_DOMAINS": '["example.com"]',
            "YANDEX_SEARCH_PUBLIC_PRODUCTS": '["Демо Pro"]',
        }
        with patch.dict(os.environ, env, clear=True), patch(
            "pm.web_search.request_once", return_value=payload
        ):
            return collect_competitor_evidence(**OFFLINE_INPUT)


def main():
    parser = argparse.ArgumentParser(description="Проверка адаптера конкурентного поиска")
    parser.add_argument("--live", action="store_true", help="Один запрос в Yandex Search API")
    parser.add_argument("--product")
    parser.add_argument("--domain")
    parser.add_argument("--topic", choices=TOPIC_TERMS)
    parser.add_argument("--snapshot", type=Path, help="Новый JSON вне репозиториев")
    args = parser.parse_args()
    if args.live:
        if not all((args.product, args.domain, args.topic)):
            parser.error("--live требует --product, --domain и --topic")
        result = _collect(product=args.product, domains=[args.domain], topics=[args.topic], smoke=True)
    else:
        if any((args.product, args.domain, args.topic)):
            parser.error("--product, --domain и --topic задаются только с --live")
        result = offline_search()
    report = {"mode": "live" if args.live else "offline", **result}
    if args.snapshot:
        try:
            write_private_json(args.snapshot, report)
        except (ValueError, OSError):
            parser.exit(2, "Укажите путь к новому JSON в существующей папке вне репозиториев.\n")
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return int(result["status"] != "ok" or (not args.live and not result["items"]))


if __name__ == "__main__":
    raise SystemExit(main())
