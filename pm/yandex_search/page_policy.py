"""Opt-in origin, DNS and robots policy for provider-discovered pages."""

import ipaddress
import re
import socket
from dataclasses import dataclass
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from pm.search_security import canonical_evidence_url, domain_name, source_url

from .client import SearchFailure
from .config import _entries, _number, enabled

BOT = "ParadiigmaEvidenceBot"
_NAT64 = tuple(map(ipaddress.ip_network, ("64:ff9b::/96", "64:ff9b:1::/48")))
_PRIVATE_PATH = re.compile(
    r"(?:^|/)(?:auth|account|login|signin|sign-in|register|checkout|cart|basket|profile|order)"
    r"(?:[/.;_-]|$)", re.I,
)
_DOWNLOAD = re.compile(r"\.(?:pdf|docx?|xlsx?|pptx?|zip|rar|7z|exe|dmg|msi|apk|gz|csv)$", re.I)
_UNRESERVED = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._~"


@dataclass(frozen=True)
class PageConfig:
    domains: frozenset[str]
    pages: int
    requests: int
    daily_limit: int
    ttl: int

    @classmethod
    def from_env(cls, allowed_domains):
        if not enabled("YANDEX_SEARCH_PAGE_FETCH_ENABLED"):
            return None
        domains = _entries("YANDEX_SEARCH_PAGE_ALLOWED_DOMAINS", domain_name)
        if not domains or not domains <= allowed_domains:
            raise ValueError("invalid_page_allowlist")
        return cls(
            domains, _number("MAX_PAGES_PER_RUN", 3, 10),
            _number("PAGE_REQUESTS_PER_RUN", 8, 25),
            _number("PAGE_DAILY_LIMIT", 30, 1000),
            _number("PAGE_CACHE_TTL", 3600, 86400, minimum=0),
        )


def checked_page_url(value, domains):
    if not isinstance(value, str) or len(value) > 2048:
        raise SearchFailure("page_url_rejected")
    try:
        host = urlsplit(value).hostname
        domain = domain_name(host)
        safe = source_url(value, domain) if domain in domains else None
    except ValueError:
        safe = None
    if not safe:
        raise SearchFailure("page_url_rejected")
    parts = urlsplit(canonical_evidence_url(safe))
    path = unquote(parts.path)
    if (
        parts.scheme != "https" or parts.query or parts.path.startswith("//")
        or re.search(r"%(?![0-9A-Fa-f]{2})|%(?:2f|5c|25|3f|23)", parts.path, re.I)
        or any(ord(char) < 32 or ord(char) == 127 for char in path)
        or any(segment in {".", ".."} for segment in path.split("/"))
        or _PRIVATE_PATH.search(path) or _DOWNLOAD.search(path)
    ):
        raise SearchFailure("page_url_rejected")
    return urlunsplit(parts._replace(path=quote(parts.path, safe="/%:@!$&'()*+,;=-._~")))


def _public_ip(raw):
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError:
        return None
    if not ip.is_global or ip.is_multicast or ip.is_reserved or ip.is_loopback:
        return None
    if ip.version == 6 and (
        ip.scope_id or ip.is_site_local or ip.ipv4_mapped or ip.sixtofour or ip.teredo
        or any(ip in network for network in _NAT64)
    ):
        return None
    return ip


def resolve_public(host):
    try:
        records = socket.getaddrinfo(host + ".", 443, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
    except OSError:
        raise SearchFailure("page_dns_unavailable") from None
    addresses = tuple(dict.fromkeys(_public_ip(record[4][0]) for record in records))
    if not addresses or None in addresses:
        raise SearchFailure("page_dns_rejected")
    return addresses[0]


def _rep_path(value):
    def normalize(match):
        char = chr(int(match[0][1:], 16))
        return char if char in _UNRESERVED else match[0].upper()

    return re.sub(r"%[0-9A-Fa-f]{2}", normalize, quote(value, safe="/%?=&:*+$,;@!()'~[]"))


def _matches(pattern, target):
    anchored = pattern.endswith("$")
    pattern = pattern.removesuffix("$")
    if "*" not in pattern:
        return target == pattern if anchored else target.startswith(pattern)
    first, *infix, last = pattern.split("*")
    if not target.startswith(first):
        return False
    pos = len(first)
    if anchored:
        if not target.endswith(last):
            return False
        end = len(target) - len(last)
    else:
        end = len(target)
        infix.append(last)
    for piece in infix:
        index = target.find(piece, pos, end)
        if index < 0:
            return False
        pos = index + len(piece)
    return pos <= end


@dataclass(frozen=True)
class Robots:
    rules: tuple = ()
    delay: float = 1.0

    def permits(self, url):
        parts = urlsplit(url)
        path = _rep_path(parts.path + ("?" + parts.query if parts.query else ""))
        match = max(
            ((len(pattern.rstrip("$").replace("*", "")), allow)
             for pattern, allow in self.rules if _matches(pattern, path)),
            default=(0, True),
        )
        return match[1]


def parse_robots(raw):
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeError:
        raise SearchFailure("page_robots_unavailable") from None
    if "\x00" in text or text.lstrip().startswith("<"):
        raise SearchFailure("page_robots_unavailable")
    groups = []
    agents, rules, delays = [], [], []
    for line in text.splitlines():
        key, sep, value = line.partition("#")[0].partition(":")
        if not sep:
            continue
        key, value = key.strip().lower(), value.strip()
        if key == "user-agent":
            if rules or delays:
                groups.append((agents, rules, delays))
                agents, rules, delays = [], [], []
            agents.append(value.lower())
        elif agents and key in {"allow", "disallow"} and value:
            rules.append((_rep_path(value), key == "allow"))
        elif agents and key in {"crawl-delay", "request-rate"}:
            if key == "crawl-delay" and re.fullmatch(r"[0-9]{1,5}(?:\.[0-9]{1,3})?", value):
                delays.append(float(value))
            elif key == "request-rate" and re.fullmatch(r"[1-9][0-9]{0,4}/[1-9][0-9]{0,4}", value):
                requests, seconds = map(int, value.split("/"))
                delays.append(seconds / requests)
            else:
                delays.append(float("inf"))
    groups.append((agents, rules, delays))
    selected = [group for group in groups if BOT.lower() in group[0]]
    selected = selected or [group for group in groups if "*" in group[0]]
    effective = tuple(rule for _, rules, _ in selected for rule in rules)
    if len(effective) > 512 or any(len(pattern) > 512 for pattern, _ in effective):
        raise SearchFailure("page_robots_unavailable")
    delay = max((value for _, _, delays in selected for value in delays), default=1.0)
    return Robots(effective, max(1.0, delay))
