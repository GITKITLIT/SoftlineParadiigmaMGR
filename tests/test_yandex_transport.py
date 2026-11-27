"""Wire-level integration using an ephemeral loopback listener and a fixture."""

import base64
import http.client
import json
import os
import tempfile
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from unittest.mock import patch

from pm.web_search import collect_competitor_evidence
from scripts.search_smoke import ROOT
from test_yandex_search import ENV, KEY, PRODUCT


class TransportIntegrationTests(unittest.TestCase):
    def test_http_wire_retry_parser_cache_and_budget(self):
        wire = []
        xml = (ROOT / "tests/fixtures/yandex/offline.xml").read_bytes()
        response = json.dumps({"rawData": base64.b64encode(xml).decode("ascii")}).encode("utf-8")

        class Provider(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                wire.append((self.path, self.headers["Authorization"], body))
                if len(wire) == 1:
                    self.send_response(503)
                    self.send_header("Retry-After", "0")
                    self.end_headers()
                else:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(response)))
                    self.end_headers()
                    self.wfile.write(response)

            def log_message(self, *_):
                pass

        with tempfile.TemporaryDirectory() as private, ThreadingHTTPServer(("127.0.0.1", 0), Provider) as server:
            thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
            thread.start()
            try:
                with patch.dict(os.environ, ENV | {"PM_PRIVATE_DIR": private}), patch(
                    "pm.yandex_search.client.http.client.HTTPSConnection",
                    side_effect=lambda host, timeout: http.client.HTTPConnection(
                        "127.0.0.1", server.server_port, timeout=timeout,
                    ),
                ) as connection:
                    args = {"product": PRODUCT, "domains": ["example.com"], "topics": ["licensing"]}
                    first = collect_competitor_evidence(**args)
                    cached = collect_competitor_evidence(**args)
            finally:
                server.shutdown()
                thread.join(timeout=2)

        self.assertEqual(first["status"], "ok")
        self.assertEqual(first["usage"], {"requests": 2, "cache_hits": 0})
        self.assertEqual(cached["usage"], {"requests": 0, "cache_hits": 1})
        self.assertEqual(first["items"], cached["items"])
        self.assertEqual(cached["metrics"]["cache_hit_rate"], 1.0)
        self.assertEqual(connection.call_count, 2)
        self.assertTrue(all(call.args[0] == "searchapi.api.cloud.yandex.net"
                            for call in connection.call_args_list))
        self.assertEqual(len(wire), 2)
        path, auth, body = wire[-1]
        self.assertEqual(path, "/v2/web/search")
        self.assertEqual(auth, "Api-Key " + KEY)
        self.assertEqual(body["query"]["queryText"],
                         'site:example.com "Демо Pro" ("лицензия"|"подписка"|"продление")')
        self.assertNotIn(KEY, json.dumps(first))


class AdditionalParserTests(unittest.TestCase):
    def test_flat_document_missing_title_and_precise_publication_date(self):
        from test_yandex_search import encoded
        from pm.yandex_search.parsing import parse_response

        payload = encoded("""<yandexsearch version="1.0"><response><results><doc>
            <url>https://example.com/product</url>
            <passages><passage>Цена <hlword>990</hlword> ₽<script>hidden</script></passage></passages>
            <published_at>2026-11-20</published_at><modtime>20261121</modtime>
            </doc></results></response></yandexsearch>""".encode())
        items, warnings = parse_response(
            payload, domain="example.com", topic="pricing", query="demo", product=PRODUCT,
            retrieved_at="2026-11-27T00:00:00Z", limit=5, secret=KEY,
        )
        item, = items
        self.assertEqual(warnings, [])
        self.assertEqual(item["title"], "example.com")
        self.assertEqual(item["text"], "Цена 990 ₽")
        self.assertIsNone(item["published_at"])
        self.assertEqual(item["published_date"], "2026-11-20")
        self.assertEqual(item["modified_date"], "2026-11-21")
