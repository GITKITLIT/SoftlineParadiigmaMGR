"""Read public pages found by WebSearch, with separate permission and budgets."""

import hashlib
import http.client
import ipaddress
import math
import sqlite3
import ssl
import time
import socket
from collections import defaultdict, deque
from itertools import islice
from urllib.parse import unquote, urljoin, urlsplit

from .client import SearchFailure, _retry_after, bounded_body, response_deadline
from .page_extract import extract_page
from .page_policy import BOT, Robots, checked_page_url, parse_robots, resolve_public
from .parsing import utc_stamp
from .storage import now_utc

_PAGE_BYTES = 512 * 1024
_TIMEOUT = 4
_REDIRECTS = {301, 302, 303, 307, 308}
_STORAGE_ERRORS = (OSError, sqlite3.Error, ValueError, RecursionError)


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, address):
        super().__init__(host, 443, timeout=_TIMEOUT, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        family = socket.AF_INET6 if self.address.version == 6 else socket.AF_INET
        raw = socket.socket(family, socket.SOCK_STREAM)
        try:
            raw.settimeout(self.timeout)
            raw.connect((str(self.address), 443))
            if ipaddress.ip_address(raw.getpeername()[0]) != self.address:
                raise SearchFailure("page_peer_rejected")
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


def _request(url, address, *, robots):
    parts = urlsplit(url)
    try:
        conn = PinnedHTTPSConnection(parts.hostname, address)
        try:
            deadline = time.monotonic() + 2 * _TIMEOUT
            conn.request("GET", parts.path, headers={
                "User-Agent": BOT + "/1.0", "Accept-Encoding": "identity",
                "Accept": "text/plain" if robots else "text/html, application/xhtml+xml",
            })
            with response_deadline(conn, deadline) as response:
                if response.status != 200:
                    retry = _retry_after(response.getheader("Retry-After")) if response.status in {429, 503} else None
                    return response.status, response.getheader("Location"), retry
                media = response.headers.get_content_type()
                if (
                    not response.getheader("Content-Type")
                    or media not in ({"text/plain"} if robots else {"text/html", "application/xhtml+xml"})
                    or response.getheader("Content-Encoding", "identity").lower() != "identity"
                    or response.getheader("Content-Disposition")
                ):
                    raise SearchFailure("page_content_rejected")
                charset = (response.headers.get_content_charset() or "utf-8").casefold()
                allowed = {"utf-8", "utf8", "windows-1251", "cp1251", "iso-8859-1"}
                if robots and charset not in {"utf-8", "utf8"} or charset not in allowed:
                    raise SearchFailure("page_content_rejected")
                raw = bounded_body(response, _PAGE_BYTES, deadline)
                return 200, None, raw if robots else raw.decode(charset)
        finally:
            conn.close()
    except TimeoutError:
        raise SearchFailure("page_timeout") from None
    except ssl.SSLError:
        raise SearchFailure("page_tls_failed") from None
    except (OSError, http.client.HTTPException, UnicodeError, ValueError):
        raise SearchFailure("page_unavailable") from None


class PageReader:
    def __init__(self, config, store, secret):
        self.config = config
        self.store = store
        self.secret = secret
        self.robots = {}
        self.addresses = {}
        self.blocked = set()
        self.usage = {"requests": 0, "cache_hits": 0}

    def _pause(self, host, seconds):
        try:
            self.store.page_pause(now_utc(), host, seconds)
        except _STORAGE_ERRORS:
            self.blocked.add(host)
            raise SearchFailure("page_storage_unavailable") from None

    def _get(self, url, *, robots=False, gap=1.0):
        host = urlsplit(url).hostname
        if host in self.blocked:
            raise SearchFailure("page_host_deferred")
        if self.usage["requests"] >= self.config.requests:
            raise SearchFailure("page_run_limit")
        address = self.addresses.get(host)
        if address is None:
            address = self.addresses[host] = resolve_public(host)
        try:
            wait, reason = self.store.page_slot(now_utc(), self.config.daily_limit, host, gap)
        except _STORAGE_ERRORS:
            raise SearchFailure("page_storage_unavailable") from None
        if reason:
            raise SearchFailure(reason)
        if wait:
            time.sleep(wait)
        self.usage["requests"] += 1
        status, location, data = _request(url, address, robots=robots)
        if status in {401, 403, 429, 503}:
            self.blocked.add(host)
        if status in {429, 503}:
            self._pause(host, max(60, data or 0))
        return status, location, data

    def _policy(self, host):
        if host not in self.robots:
            self.robots[host] = None
            status, _, raw = self._get(f"https://{host}/robots.txt", robots=True)
            if status == 200:
                policy = parse_robots(raw)
                self._pause(host, policy.delay if math.isfinite(policy.delay) else 86400)
                self.robots[host] = policy
            elif status in {404, 410}:
                self.robots[host] = Robots()
        policy = self.robots[host]
        if policy is None:
            raise SearchFailure("page_robots_unavailable")
        return policy

    def read(self, url):
        seen = set()
        for _ in range(3):
            url = checked_page_url(url, self.config.domains)
            if self.secret in unquote(url):
                raise SearchFailure("page_url_rejected")
            if url in seen or urlsplit(url).path == "/robots.txt":
                raise SearchFailure("page_redirect_rejected")
            seen.add(url)
            policy = self._policy(urlsplit(url).hostname)
            if not policy.permits(url):
                raise SearchFailure("page_robots_denied")
            status, target, html = self._get(url, gap=policy.delay)
            if status == 200:
                return url, html
            if status not in _REDIRECTS:
                raise SearchFailure("page_access_denied" if status in {401, 403, 429} else "page_unavailable")
            if not target or len(target) > 2048 or any(
                char.isspace() or ord(char) < 32 or ord(char) == 127 for char in target
            ):
                raise SearchFailure("page_redirect_rejected")
            url = urljoin(url, target)
        raise SearchFailure("page_redirect_rejected")


def _targets(items, config):
    sources = {}
    queues = defaultdict(deque)
    for item in items:
        if item["domain"] not in config.domains:
            continue
        try:
            url = checked_page_url(item["url"], config.domains)
        except SearchFailure:
            continue
        if url not in sources:
            sources[url] = []
            queues[item["domain"]].append(url)
        sources[url].append(item)
    while queues:
        for domain in tuple(queues):
            queue = queues[domain]
            url = queue.popleft()
            yield url, sources[url]
            if not queue:
                del queues[domain]


def _cached_page(reader, url, source, config, store, warnings):
    names = (source["product"], *config.aliases.get(source["product"], ()))
    key = hashlib.sha256(repr((url, names, source["topic"], 2)).encode()).hexdigest()
    with store.key_lock(key):
        try:
            cached = store.page_get(key, now_utc(), reader.config.ttl)
        except _STORAGE_ERRORS:
            raise SearchFailure("page_storage_unavailable") from None
        if cached is not None:
            if not isinstance(cached, dict) or not isinstance(cached.get("url"), str):
                raise SearchFailure("page_storage_unavailable")
            checked_page_url(cached["url"], reader.config.domains)
            reader.usage["cache_hits"] += 1
            return cached
        final_url, html = reader.read(url)
        extracted = extract_page(html, names=names, topic=source["topic"], secret=config.api_key)
        fetched = now_utc()
        record = extracted | {"retrieved_at": utc_stamp(fetched), "url": final_url}
        try:
            store.page_put(key, record, fetched, reader.config.ttl)
        except _STORAGE_ERRORS:
            warnings.append("page_cache_write_failed")
        return record


def augment_pages(result, *, page_config, search_config, store):
    reader = PageReader(page_config, store, search_config.api_key)
    result["page_usage"] = reader.usage
    added, notes = {}, list(result["warnings"])

    for url, sources in islice(_targets(result["items"], page_config), page_config.pages):
        source = sources[0]
        try:
            page = _cached_page(reader, url, source, search_config, store, notes)
            fragments, attrs = page["fragments"], page["attributes"]
            origins, conflicts = page["attribute_sources"], page["attribute_conflicts"]
            if (
                not isinstance(fragments, list) or not all(isinstance(value, str) for value in fragments)
                or not isinstance(attrs, dict) or not all(isinstance(value, str) for value in attrs.values())
                or not isinstance(origins, dict) or set(origins) != set(attrs)
                or any(value not in {"jsonld", "visible"} for value in origins.values())
                or not isinstance(conflicts, dict) or not all(
                    type(values) is list and len(values) == 2 and all(isinstance(value, str) for value in values)
                    for values in conflicts.values()
                )
                or not isinstance(page.get("title"), str) or not isinstance(page.get("retrieved_at"), str)
                or any(search_config.api_key in value for value in (
                    unquote(page["url"]), page["title"], *fragments, *attrs.values(),
                    *(value for rows in conflicts.values() for value in rows),
                ))
            ):
                raise ValueError("invalid_page_cache")
            text = "\n".join(fragments)
            markup = [f"{name}={value}" for name, value in attrs.items() if origins[name] == "jsonld"]
            markup.extend(f"{name}={values[0]}" for name, values in conflicts.items())
            if markup:
                text = (text + "\n[JSON-LD] " + " | ".join(markup)).strip()
            url = page["url"]
            digest = hashlib.sha256((url + "\0" + text).encode("utf-8")).hexdigest()[:32]
            key = "web:" + digest
            item = added.setdefault(key, {
                "id": key, "kind": "competitor_web", "ref": url, "url": url,
                "domain": urlsplit(url).hostname, "title": page["title"] or source["title"],
                "text": text[:2000], "retrieved_at": page["retrieved_at"], "published_at": None,
                "content_type": "page_excerpt", "topic": source["topic"], "query": source["query"],
                "product": source["product"], "attributes": attrs, "attribute_conflicts": conflicts,
                "provenance": {
                    "discovered_url": source["url"], "method": "static_html",
                    "search_evidence_ids": [], "checked_sections": len(fragments),
                    "attribute_sources": origins,
                },
            })
            ids = item["provenance"]["search_evidence_ids"]
            ids.extend(record["id"] for record in sources if record["id"] not in ids)
        except SearchFailure as failure:
            notes.append(failure.reason)
        except (KeyError, TypeError, ValueError):
            notes.append("page_storage_unavailable")
    result["items"] = list(added.values()) + result["items"]
    result["warnings"] = list(dict.fromkeys(notes))
