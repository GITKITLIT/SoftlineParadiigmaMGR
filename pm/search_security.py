"""Validate structured public search inputs and the URLs returned by Yandex."""

import re
import unicodedata
from urllib.parse import unquote, urlsplit, urlunsplit

TOPIC_TERMS = {
    "pricing": "цена",
    "licensing": "лицензия",
    "checkout": "оформление покупки",
    "product_features": "характеристики",
    "support": "поддержка",
}
TOPIC_SYNONYMS = {
    "pricing": ("цена", "стоимость", "купить"),
    "licensing": ("лицензия", "подписка", "продление"),
    "checkout": ("оформление заказа", "оплата", "самостоятельная покупка"),
    "product_features": ("характеристики", "версия", "ассортимент"),
    "support": ("поддержка", "консультация", "сервис"),
}
MAX_INPUT_ITEMS = 50
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", re.ASCII)
_RESERVED_SUFFIXES = {"localhost", "local", "internal", "invalid", "test", "example", "onion"}


def domain_name(value: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 253:
        raise ValueError("invalid_domain")
    value = unicodedata.normalize("NFC", value).lower().removesuffix(".")
    labels = value.split(".")
    if len(labels) < 2:
        raise ValueError("invalid_domain")
    canonical = []
    for label in labels:
        encoded = label.encode("idna").decode("ascii")
        if not _LABEL.fullmatch(encoded):
            raise ValueError("invalid_domain")
        if encoded.startswith("xn--") or not label.isascii():
            decoded = encoded.encode("ascii").decode("idna")
            if label not in {encoded, decoded}:
                raise ValueError("ambiguous_domain")
        canonical.append(encoded)
    host = ".".join(canonical)
    suffix = canonical[-1]
    if len(host) > 253 or suffix in _RESERVED_SUFFIXES or not re.match(r"[a-z]", suffix):
        raise ValueError("invalid_domain")
    return host


def product_name(value: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 120:
        raise ValueError("invalid_product")
    value = unicodedata.normalize("NFC", value)
    if any(
        char not in " ._+()-–—®™" and unicodedata.category(char)[0] not in "LMN"
        for char in value
    ):
        raise ValueError("invalid_product")
    value = " ".join(value.split())
    if not any(char.isalnum() for char in value):
        raise ValueError("invalid_product")
    return value


def public_queries(product, domains, topics, allowed_products, allowed_domains, aliases=None):
    """Return the public product and ordered (domain, topic, query) tuples."""
    if any(
        type(values) is not list or not 1 <= len(values) <= MAX_INPUT_ITEMS
        for values in (domains, topics)
    ):
        raise ValueError("invalid_input")
    product = product_name(product)
    domains = dict.fromkeys(domain_name(value) for value in domains)
    if product not in allowed_products or not domains.keys() <= allowed_domains:
        raise ValueError("invalid_input")
    if any(not isinstance(topic, str) or topic not in TOPIC_TERMS for topic in topics):
        raise ValueError("invalid_input")
    names = (product, *(aliases or {}).get(product, ()))
    names_clause = _alternatives(names)
    queries = [
        (host, topic, f"site:{host} {names_clause} {_alternatives(TOPIC_SYNONYMS[topic])}")
        for topic in dict.fromkeys(topics)
        for host in domains
    ]
    if any(len(query) > 400 or len(re.findall(r"\w+", query)) > 40 for _, _, query in queries):
        raise ValueError("query_too_long")
    return product, queries


def _alternatives(values):
    quoted = [f'"{value}"' for value in values]
    return quoted[0] if len(quoted) == 1 else "(" + "|".join(quoted) + ")"


def source_url(value: str, expected_domain: str) -> str | None:
    if not isinstance(value, str) or not value or len(value) > 2048 or any(
        char in '\\<>"{}|^`' or char.isspace() or unicodedata.category(char).startswith("C")
        for char in value
    ):
        return None
    try:
        parts = urlsplit(value)
        host = domain_name(parts.hostname)
        if (
            parts.scheme not in {"http", "https"}
            or parts.username is not None
            or parts.netloc.endswith(":")
            or parts.port not in (None, 443 if parts.scheme == "https" else 80)
            or host != expected_domain
        ):
            return None
    except ValueError:
        return None
    return urlunsplit((parts.scheme, host, parts.path or "/", parts.query, ""))


def canonical_evidence_url(url):
    parts = urlsplit(url)
    fields = []
    for field in parts.query.split("&"):
        name = unquote(field.partition("=")[0]).casefold()
        if field and not name.startswith("utm_") and name not in {"yclid", "gclid", "fbclid"}:
            fields.append(field)
    return urlunsplit(parts._replace(query="&".join(fields), fragment=""))
