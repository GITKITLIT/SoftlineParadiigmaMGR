"""Measure a full local HTTP request and capture usage without research excerpts."""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pm.settings import private_dir

p = argparse.ArgumentParser()
p.add_argument("--port", type=int, default=8080)
p.add_argument("--output", type=Path, default=private_dir() / "benchmark.json")
p.add_argument("--query", default="Как проверить событие добавления лицензии в корзину?")
args = p.parse_args()
question = args.query
start = perf_counter()
req = Request(
    f"http://127.0.0.1:{args.port}/api/analyze",
    data=json.dumps({"query": question}).encode(),
    headers={"Content-Type": "application/json"},
)
with urlopen(req, timeout=180) as stream:
    r = json.load(stream)
elapsed = perf_counter() - start
if not r.get("model") or "usage" not in r:
    raise SystemExit("Полный вызов модели не подтверждён: " + str(r.get("notice")))
record = {k: r[k] for k in ("mode", "model", "hypothesis", "proposal", "refs", "usage")}
record.update(
    seconds=round(elapsed, 3),
    query=question,
    measured_utc=datetime.now(timezone.utc).isoformat(),
    calls=1,
    source="HTTP POST /api/analyze · данные → поиск → Qwen → проверка → ответ",
)
args.output.parent.mkdir(parents=True, exist_ok=True)
args.output.write_text(
    json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf8"
)
print(args.output)
