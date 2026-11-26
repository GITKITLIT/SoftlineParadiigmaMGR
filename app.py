"""Run the local closed-loop product assistant or build an offline report."""

import argparse
import hashlib
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from pm.analytics import compile_book, effect, finite
from pm.research import analyze, evidence, index_range, search, sources
from pm.settings import ROOT, private_dir
from pm.service import metrika, overview, recalculate

LOCK = threading.Lock()


def as_json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def page(data=None):
    data = data or overview()
    benchmark = os.getenv("PM_BENCHMARK")
    example = json.loads(Path(benchmark).read_text(encoding="utf-8-sig")) if benchmark else None
    html = (ROOT / "web/index.html").read_text(encoding="utf8")
    return (
        html.replace("/*STYLE*/", (ROOT / "web/style.css").read_text(encoding="utf8"))
        .replace("/*SCRIPT*/", (ROOT / "web/app.js").read_text(encoding="utf8"))
        .replace(
            "/*BOOT*/",
            as_json(
                dict(
                    data=data,
                    evidence=evidence(),
                    example=example,
                )
            ).replace("<", "\\u003c"),
        )
    )


def local_doc(body):
    name, txt = str(body.get("name", "")).strip(), str(body.get("text", "")).strip()
    kind = body.get("kind")
    if (
        not name
        or len(name) > 120
        or not 5 <= len(txt) <= 30000
        or "эталон" in name.casefold()
        or kind not in ("Интервью", "Конкуренты")
    ):
        raise ValueError(
            "Укажите название и до 30 000 символов исследовательского текста"
        )
    key = hashlib.sha256((kind + "|" + name).encode()).hexdigest()[:12]
    lines = [s.strip() for s in txt.splitlines() if s.strip()]
    adds = [
        dict(id=f"u-{key}-{i}", ref=f"{name} · абзац {i}", text=line[:2000], kind=kind)
        for i, line in enumerate(lines, 1)
    ]
    if any(len(line) > 2000 for line in lines):
        raise ValueError("Разбейте текст на абзацы до 2000 символов")
    with LOCK:
        local = private_dir() / "sources.json"
        original = (
            json.loads(local.read_text(encoding="utf8")) if local.exists() else []
        )
        merged = [x for x in original if not x["id"].startswith(f"u-{key}-")] + adds
        local.parent.mkdir(parents=True, exist_ok=True)
        tmp = local.with_suffix(".tmp")
        tmp.write_text(as_json(merged), encoding="utf8")
        tmp.replace(local)
    return {"added": len(adds)}


def api(route, b):
    if route == "/api/overview":
        return recalculate(overview(), b)
    if route == "/api/search":
        return {"hits": search(str(b["query"])[:500], sources())}
    if route == "/api/import":
        return local_doc(b)
    if route == "/api/analyze":
        return analyze(
            str(b["query"])[:500], recalculate(overview(), b.get("config", {}))
        )
    if route == "/api/metrika":
        return metrika(b["start"], b["end"])
    if route == "/api/effect":
        if b["mode"] == "demo":
            s = json.loads((ROOT / "data/effect-demo.json").read_text(encoding="utf8"))
        elif b["mode"] == "actual":
            s = b.get("observation") or {}
            if not s:
                return {"status": "Ожидаем данные после исправления"}
        else:
            raise ValueError("Неизвестный источник")
        return effect(s["rows"], s["fix"], finite(b["target"]))
    raise KeyError("route")


class Handler(BaseHTTPRequestHandler):
    def is_local(self):
        host = self.headers.get("Host", "")
        return (
            host.split(":")[0] in ("127.0.0.1", "localhost")
            and self.headers.get("Origin", "http://" + host) == "http://" + host
        )

    def reply(self, status, ctype, body):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self.is_local():
            self.reply(403, "text/plain", b"Local requests only")
            return
        path = urlsplit(self.path).path
        if path == "/":
            self.reply(200, "text/html; charset=utf-8", page().encode())
        elif path == "/cost.xlsx" and (ROOT / "build/cost.xlsx").is_file():
            self.reply(
                200,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                (ROOT / "build/cost.xlsx").read_bytes(),
            )
        else:
            self.reply(404, "text/plain", b"Not found")

    def do_POST(self):
        status = 200
        try:
            if not self.is_local():
                self.reply(403, "text/plain", b"Local requests only")
                return
            if self.headers.get_content_type() != "application/json":
                raise ValueError("Требуется JSON")
            n = int(self.headers.get("Content-Length", 0))
            if not 0 < n <= 150000:
                raise ValueError("Некорректный размер запроса")
            body = json.loads(self.rfile.read(n))
            if not isinstance(body, dict):
                raise ValueError("Требуется объект")
            result = api(urlsplit(self.path).path, body)
        except (ValueError, KeyError, TypeError, OSError, IndexError) as error:
            status = 400
            result = {
                "error": str(error)[:200]
                if isinstance(error, ValueError)
                else "Проверьте поля запроса и входные данные"
            }
        self.reply(status, "application/json; charset=utf-8", as_json(result).encode())

    def log_message(self, format, *args):
        pass


def main():
    p = argparse.ArgumentParser(description=__doc__)
    subs = p.add_subparsers(dest="cmd", required=True)
    server = subs.add_parser("serve")
    server.add_argument("--port", type=int, default=8080)
    server.add_argument("--workbook")
    build = subs.add_parser("build")
    build.add_argument("--source-label", default="Локальная выгрузка")
    build.add_argument("--workbook")
    build.add_argument("--config")
    build.add_argument("--output", default="build/index.html")
    imp = subs.add_parser("import")
    for opt in ("file", "sheet", "range", "kind"):
        imp.add_argument("--" + opt, required=True)
    args = p.parse_args()
    if args.cmd == "serve":
        if args.workbook:
            os.environ["PM_WORKBOOK"] = args.workbook
        overview()
        print(f"ParadiigmaMGR: http://127.0.0.1:{args.port}", flush=True)
        ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()
    elif args.cmd == "import":
        print("Добавлено:", index_range(args.file, args.sheet, args.range, args.kind))
    else:
        dest = Path(args.output).resolve()
        has_private_inputs = args.workbook or any(
            os.getenv(name)
            for name in ("PM_WORKBOOK", "PM_SNAPSHOT", "PM_EVIDENCE", "PM_BENCHMARK")
        )
        if has_private_inputs and dest.is_relative_to(ROOT):
            p.error("Укажите --output за пределами репозитория для закрытых данных.")
        cfg = (
            json.loads(Path(args.config).read_text(encoding="utf8"))
            if args.config
            else {}
        )
        snapshot = (
            compile_book(args.workbook, cfg, args.source_label)
            if args.workbook
            else recalculate(overview(), cfg)
        )
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(page(snapshot), encoding="utf8")
        print(dest)


if __name__ == "__main__":
    main()
