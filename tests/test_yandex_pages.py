import ipaddress
import json
import os
import socket
import ssl
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pm.web_search import _collect, collect_competitor_evidence
from pm.yandex_search.client import SearchFailure
from pm.yandex_search.page_extract import extract_page
from pm.yandex_search.page_policy import BOT, checked_page_url, parse_robots, resolve_public
from pm.yandex_search.pages import PinnedHTTPSConnection
from test_yandex_search import DOC, ENV, KEY, PRODUCT, reply

HTML = (Path(__file__).parent / "fixtures/yandex/competitor-page.html").read_text(encoding="utf-8")
VALID = ipaddress.ip_address("8.8.8.8")
BAD_IPS = ("127.0.0.1", "10.1.2.3", "169.254.169.254", "100.64.0.1", "224.0.0.3",
           "::1", "fc00::1", "::ffff:127.0.0.1", "64:ff9b::a00:1", "2002:7f00:1::1")


class PolicyTests(unittest.TestCase):
    def test_strict_url_policy_and_tracking_normalization(self):
        domains = {"example.com"}
        self.assertEqual(checked_page_url("https://example.com/p?utm_source=ya#x", domains),
                         "https://example.com/p")
        bad = ("http://example.com/p", "https://example.com/a?session=x",
               "https://other.example.com/p", "https://u@example.com/p", "https://example.com:444/",
               "https://example.com/%6cogin", "https://example.com/a/%2e%2e/b",
               "https://example.com/a%2fok", "https://example.com/a%250a",
               "https://example.com/a%0a", "https://example.com/order/123",
               "https://example.com/file%2epdf", "https://example.com//example.org/a")
        for url in bad:
            with self.subTest(url=url), self.assertRaises(SearchFailure):
                checked_page_url(url, domains)

    def test_mixed_and_private_dns_are_blocked(self):
        def record(addr):
            family = socket.AF_INET6 if ":" in addr else socket.AF_INET
            return family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (addr, 443)

        for ip in BAD_IPS:
            with self.subTest(ip=ip), patch(
                "pm.yandex_search.page_policy.socket.getaddrinfo", return_value=[record(str(VALID)), record(ip)],
            ), self.assertRaisesRegex(SearchFailure, "page_dns_rejected"):
                resolve_public("example.com")
        with patch("pm.yandex_search.page_policy.socket.getaddrinfo",
                   return_value=[record(str(VALID))]) as lookup:
            self.assertEqual(resolve_public("example.com"), VALID)
            self.assertEqual(lookup.call_args.args[0], "example.com.")

    def test_tls_pins_numeric_peer_and_keeps_original_hostname(self):
        connection = PinnedHTTPSConnection("example.com", VALID)
        self.assertTrue(connection._context.check_hostname)
        self.assertEqual(connection._context.verify_mode, ssl.CERT_REQUIRED)
        with patch("pm.yandex_search.pages.socket.socket") as create, patch.object(
            connection._context, "wrap_socket",
        ) as wrap:
            raw = create.return_value
            raw.getpeername.return_value = (str(VALID), 443)
            connection.connect()
            raw.connect.assert_called_once_with((str(VALID), 443))
            wrap.assert_called_once_with(raw, server_hostname="example.com")
            raw.getpeername.return_value = ("10.0.0.1", 443)
            with self.assertRaisesRegex(SearchFailure, "page_peer_rejected"):
                connection.connect()
            raw.close.assert_called_once()
            self.assertEqual(wrap.call_count, 1)

    def test_robots_groups_wildcard_anchor_and_delay(self):
        robots = parse_robots(f"""
User-agent: *
Disallow: /
User-agent: {BOT}
Disallow: /docs*
Allow: /docs/public$
Crawl-delay: 2
User-agent: {BOT}
Disallow: /%73ecret
Request-rate: 1/3
""".encode())
        self.assertEqual(robots.delay, 3)
        self.assertTrue(robots.permits("https://example.com/docs/public"))
        self.assertFalse(robots.permits("https://example.com/docs/publicx"))
        self.assertFalse(robots.permits("https://example.com/secret"))
        self.assertTrue(robots.permits("https://example.com/safe"))


class PageIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.private = self.enterContext(tempfile.TemporaryDirectory())
        self.env = self.enterContext(patch.dict(os.environ, ENV | {
            "PM_PRIVATE_DIR": self.private, "YANDEX_SEARCH_PAGE_FETCH_ENABLED": "true",
            "YANDEX_SEARCH_PAGE_ALLOWED_DOMAINS": '["example.com"]',
            "YANDEX_SEARCH_MAX_PAGES_PER_RUN": "3", "YANDEX_SEARCH_PAGE_REQUESTS_PER_RUN": "8",
            "YANDEX_SEARCH_PAGE_DAILY_LIMIT": "30", "YANDEX_SEARCH_PAGE_CACHE_TTL": "3600",
        }))
        self.enterContext(patch("pm.web_search.request_once", return_value=reply(DOC)))
        self.resolve = self.enterContext(patch("pm.yandex_search.pages.resolve_public", return_value=VALID))
        self.request = self.enterContext(patch("pm.yandex_search.pages._request"))
        self.enterContext(patch("pm.yandex_search.pages.time.sleep"))
        self.args = {"product": PRODUCT, "domains": ["example.com"], "topics": ["licensing"]}

    def test_only_opt_in_pages_and_live_smoke_never_fetches_pages(self):
        self.env["YANDEX_SEARCH_PAGE_FETCH_ENABLED"] = "false"
        self.assertNotIn("page_usage", collect_competitor_evidence(**self.args))
        self.env["YANDEX_SEARCH_PAGE_FETCH_ENABLED"] = "true"
        self.env["YANDEX_SEARCH_LIVE_SMOKE"] = "true"
        self.assertNotIn("page_usage", _collect(**self.args, smoke=True))
        self.resolve.assert_not_called()
        self.request.assert_not_called()

    def test_enrich_and_cache_with_provenance_and_separate_budget(self):
        self.request.side_effect = [(404, None, None), (200, None, HTML)]
        result = collect_competitor_evidence(**self.args)
        page, snippet = result["items"]
        self.assertEqual(page["content_type"], "page_excerpt")
        self.assertEqual(snippet["content_type"], "snippet")
        self.assertIn("Консультация", page["text"])
        self.assertEqual(page["attributes"]["price"], "1050.50")
        self.assertEqual(page["attributes"]["currency"], "RUB")
        self.assertEqual(page["attributes"]["devices"], "1")
        self.assertEqual(page["provenance"]["search_evidence_ids"], [snippet["id"]])
        for prohibited in ("HIDDEN", "Ignore", "SCRIPT", "Скрытое меню", KEY):
            self.assertNotIn(prohibited, json.dumps(page, ensure_ascii=False))
        self.assertEqual(self.request.call_count, 2)
        self.assertEqual(result["page_usage"]["requests"], 2)
        self.assertEqual(result["usage"]["requests"], 1)
        cached = collect_competitor_evidence(**self.args)
        self.assertEqual(cached["page_usage"], {"requests": 0, "cache_hits": 1})
        self.assertEqual(cached["items"], result["items"])

    def test_robots_denial_does_not_fetch_page(self):
        self.request.return_value = (200, None, b"User-agent: *\nDisallow: /product")
        result = collect_competitor_evidence(**self.args)
        self.assertIn("page_robots_denied", result["warnings"])
        self.assertEqual(result["items"][0]["content_type"], "snippet")
        self.request.assert_called_once()

    def test_unlisted_redirect_cannot_resolve_or_send(self):
        self.request.side_effect = [(404, None, None), (302, "https://unexpected.example.net/p", None)]
        result = collect_competitor_evidence(**self.args)
        self.assertIn("page_url_rejected", result["warnings"])
        self.resolve.assert_called_once_with("example.com")
        self.assertEqual(self.request.call_count, 2)

    def test_retry_after_defers_host_across_separate_collections(self):
        self.request.side_effect = [(404, None, None), (429, None, 600)]
        first = collect_competitor_evidence(**self.args)
        self.assertIn("page_access_denied", first["warnings"])
        second = collect_competitor_evidence(**self.args)
        self.assertIn("page_rate_deferred", second["warnings"])
        self.assertEqual(second["page_usage"]["requests"], 0)
        self.assertEqual(self.request.call_count, 2)

    def test_conflicting_visible_and_structured_prices_are_retained_for_review(self):
        html = HTML.replace(
            "</main>", "<p>Продукт: Демо Pro | Цена: 2200 | Валюта: RUB</p></main>",
        )
        result = extract_page(html, names=(PRODUCT,), topic="pricing", secret=KEY)
        self.assertNotIn("price", result["attributes"])
        self.assertEqual(result["attribute_conflicts"]["price"], ["1050.50", "2200"])

    def test_page_budget_covers_robots_and_prevents_second_request(self):
        self.env["YANDEX_SEARCH_PAGE_REQUESTS_PER_RUN"] = "1"
        self.request.return_value = (404, None, None)
        result = collect_competitor_evidence(**self.args)
        self.assertIn("page_run_limit", result["warnings"])
        self.request.assert_called_once()

    def test_captcha_fallback(self):
        with self.assertRaisesRegex(SearchFailure, "page_access_challenge"):
            extract_page("<title>Verify that you are human</title><main>Demo</main>",
                         names=(PRODUCT,), topic="licensing", secret=KEY)
