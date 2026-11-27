import base64
import io
import json
import os
import ssl
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

from pm.search_security import domain_name, source_url
from pm.web_search import _collect, collect_competitor_evidence
from pm.yandex_search.client import (
    MAX_HTTP_BYTES, SearchFailure, request_once, retry_delay,
)
from pm.yandex_search.config import Config
from pm.yandex_search.parsing import MAX_XML_BYTES
from pm.yandex_search.storage import Store


PRODUCT = "Демо Pro"
KEY = "synthetic-do-not-record"
ENV = {
    "YANDEX_SEARCH_ENABLED": "true",
    "YANDEX_SEARCH_FOLDER_ID": "synthetic-folder",
    "YANDEX_SEARCH_API_KEY": KEY,
    "YANDEX_SEARCH_ALLOWED_DOMAINS": '["example.com", "shop.example.com"]',
    "YANDEX_SEARCH_PUBLIC_PRODUCTS": '["Демо Pro"]',
    "YANDEX_SEARCH_PUBLIC_ALIASES": "{}",
    "YANDEX_SEARCH_PAGE_FETCH_ENABLED": "false",
    "YANDEX_SEARCH_MAX_RESULTS": "5",
    "YANDEX_SEARCH_MAX_QUERIES_PER_RUN": "5",
    "YANDEX_SEARCH_DAILY_LIMIT": "20",
    "YANDEX_SEARCH_CACHE_TTL": "86400",
    "YANDEX_SEARCH_LIVE_SMOKE": "false",
}
DOC = """<doc><url>https://EXAMPLE.COM:443/product#price</url>
    <domain>forged.example.org</domain><title>Демо <hlword>Pro</hlword></title>
    <passages><passage>Лицензия <hlword>12</hlword> месяцев</passage></passages>
    <modtime>2026-11-20T12:00:00+03:00</modtime>
</doc>"""


def encoded(raw):
    return json.dumps({"rawData": base64.b64encode(raw).decode("ascii")}).encode()


def reply(docs=""):
    return encoded(f"""<?xml version="1.0" encoding="UTF-8"?>
    <yandexsearch xmlns="urn:synthetic"><response>
    <results><grouping><group>{docs}</group></grouping></results>
    </response></yandexsearch>""".encode())


def xml_error(code):
    return encoded(
        f'<yandexsearch><response><error code="{code}">{KEY}</error></response></yandexsearch>'
        .encode()
    )


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.env = self.enterContext(patch.dict(os.environ, ENV | {
            "PM_PRIVATE_DIR": str(self.directory),
        }))
        self.request = self.enterContext(patch("pm.web_search.request_once", return_value=reply(DOC)))
        self.sleep = self.enterContext(patch("pm.web_search.time.sleep"))

    def call(self, **kwargs):
        return collect_competitor_evidence(**(
            {"product": PRODUCT, "domains": ["example.com"], "topics": ["licensing"]} | kwargs
        ))

    def test_v1_cache_stable_provenance(self):
        first = self.call(domains=["Example.COM.", "example.com"])
        second = self.call(product="Демо  Pro")
        self.assertEqual(first["schema_version"], 1)
        self.assertEqual(first["status"], "ok")
        self.assertIsNone(first["reason"])
        self.assertEqual(first["source"], "yandex_search")
        self.assertEqual(first["usage"], {"requests": 1, "cache_hits": 0})
        self.assertEqual(second["usage"], {"requests": 0, "cache_hits": 1})
        self.assertEqual(first["items"], second["items"])
        item, = first["items"]
        self.assertEqual(item["title"], "Демо Pro")
        self.assertEqual(item["text"], "Лицензия 12 месяцев")
        self.assertEqual(item["ref"], "https://example.com/product")
        self.assertEqual(item["domain"], "example.com")
        self.assertEqual(item["query"], 'site:example.com "Демо Pro" ("лицензия"|"подписка"|"продление")')
        self.assertEqual(item["modified_at"], "2026-11-20T09:00:00Z")
        self.assertIsNone(item["published_at"])
        self.assertEqual(item["content_type"], "snippet")
        self.assertTrue(item["retrieved_at"].endswith("Z"))
        self.assertTrue(item["id"].startswith("web:"))
        self.assertNotIn(KEY.encode(), (self.directory / "yandex-search.sqlite3").read_bytes())
        self.request.assert_called_once()

    def test_empty_results_and_expiry(self):
        self.env["YANDEX_SEARCH_CACHE_TTL"] = "10"
        self.request.side_effect = [xml_error(15), reply()]
        start = datetime(2026, 11, 27, tzinfo=UTC)
        with patch("pm.web_search.now_utc", return_value=start) as clock:
            first = self.call()
            clock.return_value = start + timedelta(seconds=9)
            cached = self.call()
            clock.return_value = start + timedelta(seconds=10)
            expired = self.call()
        self.assertEqual([r["status"] for r in (first, cached, expired)], ["ok"] * 3)
        self.assertEqual([r["items"] for r in (first, cached, expired)], [[], [], []])
        self.assertEqual(cached["usage"]["cache_hits"], 1)
        self.assertEqual(expired["usage"]["requests"], 1)
        self.assertEqual(expired["metrics"]["expired_cache_entries"], 1)
        self.assertIn("expired_cache", expired["warnings"])

    def test_invalid_inputs_and_no_partial_validation(self):
        for changes in (
            {"product": "Демо Pro OR site:evil.com"},
            {"product": "Интервью и продажи 153"},
            {"product": "Демо\nPro"},
            {"product": 42},
            {"domains": ["example.com", "other.example.com"]},
            {"domains": ["example.com@evil.com"]},
            {"domains": ["example.com:443"]},
            {"domains": ["example.com/"]},
            {"domains": ["*.example.com"]},
            {"domains": ["127.0.0.1"]},
            {"domains": ("example.com",)},
            {"topics": ["licensing", "TASK-123"]},
            {"topics": [{}]},
            {"topics": []},
        ):
            with self.subTest(changes=changes):
                self.assertEqual(self.call(**changes)["reason"], "invalid_input")
        self.request.assert_not_called()
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_public_aliases_and_domain_coverage_with_small_budget(self):
        self.env["YANDEX_SEARCH_PUBLIC_ALIASES"] = '{"Демо Pro":["Demo Professional","Демо Pro 2026"]}'
        self.env["YANDEX_SEARCH_MAX_QUERIES_PER_RUN"] = "2"
        result = self.call(domains=["example.com", "shop.example.com"], topics=["checkout", "support"])
        args = [call.args[0] for call in self.request.call_args_list]
        self.assertEqual(len(args), 2)
        self.assertIn('("Демо Pro"|"Demo Professional"|"Демо Pro 2026")', args[0])
        self.assertIn('"самостоятельная покупка"', args[0])
        self.assertTrue(args[1].startswith("site:shop.example.com "))
        self.assertEqual([x["status"] for x in result["coverage"]], ["ok", "ok", "skipped", "skipped"])
        self.assertEqual(result["status"], "partial")
        self.env["YANDEX_SEARCH_PUBLIC_ALIASES"] = '{"Демо Pro":["Demo\" site:evil.com"]}'
        self.assertEqual(self.call()["reason"], "invalid_configuration")
        self.assertEqual(self.request.call_count, 2)

    def test_rank_product_listing_and_remove_tracking_duplicates(self):
        self.env["YANDEX_SEARCH_MAX_RESULTS"] = "1"
        low = DOC.replace("Демо <hlword>Pro</hlword>", "Прочие товары").replace("/product", "/blog")
        self.request.return_value = reply(low + DOC + DOC.replace("#price", "?utm_source=search"))
        item, = self.call()["items"]
        self.assertEqual(item["title"], "Демо Pro")
        self.assertGreater(item["relevance"], 8)

    def test_fail_closed_configuration(self):
        for name, value, reason in (
            ("YANDEX_SEARCH_ENABLED", "false", "search_disabled"),
            ("YANDEX_SEARCH_ENABLED", "yes", "invalid_configuration"),
            ("YANDEX_SEARCH_ALLOWED_DOMAINS", '["example.com",null]', "invalid_configuration"),
            ("YANDEX_SEARCH_PUBLIC_PRODUCTS", "[]", "allowlist_not_configured"),
            ("YANDEX_SEARCH_API_KEY", "", "missing_credentials"),
            ("YANDEX_SEARCH_FOLDER_ID", "", "missing_credentials"),
            ("YANDEX_SEARCH_API_KEY", "bad\nkey", "invalid_configuration"),
            ("YANDEX_SEARCH_CACHE_TTL", "-1", "invalid_configuration"),
            ("YANDEX_SEARCH_MAX_RESULTS", "101", "invalid_configuration"),
        ):
            with self.subTest(name=name, value=value), patch.dict(os.environ, {name: value}):
                result = self.call()
                self.assertEqual((result["status"], result["reason"]), ("disabled", reason))
        self.request.assert_not_called()

    def test_idna_exact_hosts(self):
        self.assertEqual(domain_name("ПРИМЕР.РФ."), "xn--e1afmkfd.xn--p1ai")
        self.assertEqual(domain_name("xn--e1afmkfd.xn--p1ai"), "xn--e1afmkfd.xn--p1ai")
        for host in ("www。example.com", "ß.de", "xn--invalid-.com", "a..com", "www.local"):
            with self.subTest(host=host), self.assertRaises(ValueError):
                domain_name(host)
        for url in (
            "javascript://example.com", "https://example.com@evil.com/",
            "https://user@example.com/", "https://example.com:8443/",
            "https://shop.example.com/", "https://example.com:","https://example.com/\\a",
        ):
            self.assertIsNone(source_url(url, "example.com"))

    def test_filter_wrong_url_host_and_secret_echo_and_dedupe(self):
        forged = DOC.replace("EXAMPLE.COM", "example.org").replace("forged.example.org", "example.com")
        private = DOC.replace("месяцев", KEY)
        self.request.return_value = reply(DOC + forged + private + DOC)
        result = self.call(topics=["licensing", "pricing"])
        item, = result["items"]
        self.assertEqual([m["topic"] for m in item["matches"]], ["licensing", "pricing"])
        self.assertEqual(result["warnings"], ["result_filtered"])
        self.assertNotIn(KEY, json.dumps(result))

    def test_encoded_markup_and_percent_encoded_secret_are_filtered(self):
        escaped = DOC.replace("месяцев", "&lt;script&gt;invalid&lt;/script&gt;")
        encoded_secret = DOC.replace("/product", "/product?token=%73" + KEY[1:])
        self.request.return_value = reply(escaped + encoded_secret)
        result = self.call()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["items"], [])
        self.assertEqual(result["warnings"], ["result_filtered"])

    def test_malformed_responses(self):
        self.env["YANDEX_SEARCH_CACHE_TTL"] = "0"
        self.env["YANDEX_SEARCH_DAILY_LIMIT"] = "50"
        for data, reason in (
            (b"{}", "invalid_response"),
            (b'{"rawData":"*"}', "invalid_base64"),
            (b'{"rawData":"AAAA\\n"}', "invalid_base64"),
            (encoded(b"<wrong/>"), "invalid_xml"),
            (encoded(b"<yandexsearch>"), "invalid_xml"),
            (encoded(b'<!DOCTYPE yandexsearch [<!ENTITY leak "test">]><yandexsearch/>'), "invalid_xml"),
            (encoded(b'<yandexsearch/>'.decode().encode("utf-16")), "invalid_xml"),
            (encoded(b"x" * (MAX_XML_BYTES + 1)), "response_too_large"),
            (b"x" * (MAX_HTTP_BYTES + 1), "response_too_large"),
            (xml_error(18), "provider_xml_error"),
        ):
            with self.subTest(reason=reason):
                self.request.return_value = data
                result = self.call()
                self.assertEqual((result["status"], result["reason"]), ("error", reason))
                self.assertNotIn(KEY, json.dumps(result))
                self.assertEqual(result["usage"]["requests"], 1)

    def test_retry_and_partial_run_budget(self):
        self.env["YANDEX_SEARCH_MAX_QUERIES_PER_RUN"] = "2"
        self.request.side_effect = [SearchFailure("timeout", "unavailable", retry=True), reply(DOC)]
        result = self.call(topics=["licensing", "pricing", "support"])
        self.assertEqual((result["status"], result["reason"]), ("partial", "run_limit"))
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["usage"]["requests"], 2)
        self.sleep.assert_called_once()

    def test_one_bad_topic_leaves_other_results(self):
        self.request.side_effect = [xml_error(18), reply(DOC)]
        result = self.call(topics=["pricing", "licensing"])
        self.assertEqual((result["status"], result["reason"]), ("partial", "provider_xml_error"))
        self.assertEqual(result["items"][0]["topic"], "licensing")

    def test_long_provider_cooldown_stops_live_for_run(self):
        self.request.side_effect = SearchFailure("rate_limited", "unavailable", retry=True, retry_after=60)
        self.assertEqual(self.call(topics=["licensing", "pricing"])["reason"], "rate_limited")
        self.request.assert_called_once()
        self.sleep.assert_not_called()

    def test_attempts_count_against_persistent_limit(self):
        self.env["YANDEX_SEARCH_DAILY_LIMIT"] = "2"
        self.request.side_effect = [SearchFailure("timeout", "unavailable", retry=True), reply(DOC)]
        self.assertEqual(self.call()["usage"]["requests"], 2)
        self.assertEqual(self.call(topics=["pricing"])["reason"], "daily_limit")
        self.assertEqual(self.call()["usage"]["cache_hits"], 1)
        self.assertEqual(self.request.call_count, 2)

    def test_concurrent_budget_reservation_and_next_utc_day(self):
        store = Store()
        now = datetime(2026, 11, 27, 23, 59, tzinfo=UTC)
        with ThreadPoolExecutor(max_workers=8) as pool:
            granted = list(pool.map(lambda _: Store().reserve(now, 7), range(28)))
        self.assertEqual(sum(granted), 7)
        self.assertTrue(store.reserve(now + timedelta(minutes=1), 7))

    def test_concurrent_runs_share_one_provider_attempt(self):
        ready = Barrier(6)

        def fetch(_):
            ready.wait(timeout=5)
            return self.call()

        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(fetch, range(6)))
        self.assertTrue(all(result["status"] == "ok" for result in results))
        self.assertEqual(sum(result["usage"]["requests"] for result in results), 1)
        self.assertEqual(sum(result["usage"]["cache_hits"] for result in results), 5)
        self.request.assert_called_once()

    def test_corrupted_cached_shape_is_a_safe_error(self):
        self.assertEqual(self.call()["status"], "ok")
        with closing(sqlite3.connect(self.directory / "yandex-search.sqlite3")) as db, db:
            db.execute("UPDATE cache SET payload = ?", ('[[{"id":"bad"}], []]',))
        result = self.call()
        self.assertEqual((result["status"], result["reason"]), ("unavailable", "storage_unavailable"))
        self.assertEqual(result["usage"]["requests"], 0)
        self.request.assert_called_once()

    def test_unusable_storage_fails_closed(self):
        (self.directory / "yandex-search.sqlite3").mkdir()
        self.assertEqual(self.call()["reason"], "storage_unavailable")
        self.request.assert_not_called()

    def test_live_smoke_flag_one_query_and_no_retry(self):
        args = {"product": PRODUCT, "domains": ["example.com"], "topics": ["pricing"], "smoke": True}
        self.assertEqual(_collect(**args)["reason"], "live_smoke_disabled")
        self.env["YANDEX_SEARCH_LIVE_SMOKE"] = "true"
        self.assertEqual(_collect(**(args | {"topics": ["pricing", "support"]}))["reason"],
                         "smoke_requires_one_query")
        self.request.assert_not_called()
        self.request.side_effect = SearchFailure("timeout", "unavailable", retry=True)
        self.assertEqual(_collect(**args)["usage"]["requests"], 1)
        self.request.assert_called_once()
        self.sleep.assert_not_called()


class ClientTests(unittest.TestCase):
    def setUp(self):
        with patch.dict(os.environ, ENV):
            self.config = Config.from_env()
        self.connection = self.enterContext(patch("pm.yandex_search.client.http.client.HTTPSConnection"))
        self.conn = self.connection.return_value

    def response(self, status, content=b"", retry_after=None):
        class Response(io.BytesIO):
            def getheader(self, field):
                return retry_after if field == "Retry-After" else None
        response = Response(content)
        response.status = status
        self.conn.getresponse.return_value = response
        return response

    def test_wire_contract_and_close(self):
        response = self.response(200, reply(DOC))
        self.assertEqual(request_once("query", self.config), reply(DOC))
        self.connection.assert_called_once_with("searchapi.api.cloud.yandex.net", timeout=5)
        args, kwargs = self.conn.request.call_args
        self.assertEqual(args, ("POST", "/v2/web/search"))
        self.assertEqual(kwargs["headers"], {
            "Authorization": "Api-Key " + KEY, "Content-Type": "application/json",
        })
        payload = json.loads(kwargs["body"])
        self.assertEqual(payload, {
            "query": {"searchType": "SEARCH_TYPE_RU", "queryText": "query", "fixTypoMode": "FIX_TYPO_MODE_OFF"},
            "folderId": "synthetic-folder", "responseFormat": "FORMAT_XML",
            "groupSpec": {"groupMode": "GROUP_MODE_FLAT", "groupsOnPage": "10", "docsInGroup": "1"},
        })
        self.assertNotIn(KEY, repr(self.config))
        self.assertTrue(response.closed)
        self.conn.close.assert_called_once()

    def test_statuses_and_response_size(self):
        for code, reason, retry in (
            (302, "redirect_rejected", False), (400, "request_rejected", False),
            (401, "authorization_failed", False), (403, "authorization_failed", False),
            (429, "rate_limited", True), (503, "provider_unavailable", True),
            (200, "response_too_large", False),
        ):
            response = self.response(code, b"x" * (MAX_HTTP_BYTES + 1) if code == 200 else KEY.encode(), "2")
            with self.subTest(code=code), self.assertRaises(SearchFailure) as cm:
                request_once("query", self.config)
            self.assertEqual((cm.exception.reason, cm.exception.retry), (reason, retry))
            if retry:
                self.assertGreaterEqual(retry_delay(cm.exception, 0), 2)
            self.assertTrue(response.closed)

    def test_timeout_and_tls_without_secret_details(self):
        for exception, reason in ((TimeoutError(KEY), "timeout"), (ssl.SSLError(KEY), "tls_failed")):
            self.conn.request.side_effect = exception
            with self.assertRaises(SearchFailure) as cm:
                request_once("query", self.config)
            self.assertEqual(str(cm.exception), reason)
