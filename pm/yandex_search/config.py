import json
import os
import re
from dataclasses import dataclass, field

from pm.search_security import MAX_INPUT_ITEMS, domain_name, product_name


def enabled(name):
    value = os.getenv(name, "false").lower()
    if value not in {"true", "false"}:
        raise ValueError("invalid_flag")
    return value == "true"


def _entries(name, normalize):
    raw = os.getenv(name, "[]")
    if len(raw) > 16384:
        raise ValueError("invalid_allowlist")
    values = json.loads(raw)
    if type(values) is not list or len(values) > MAX_INPUT_ITEMS:
        raise ValueError("invalid_allowlist")
    return frozenset(normalize(value) for value in values)


def _number(name, default, ceiling, *, minimum=1):
    raw = os.getenv("YANDEX_SEARCH_" + name, str(default))
    if not re.fullmatch(r"[0-9]{1,8}", raw) or not minimum <= int(raw) <= ceiling:
        raise ValueError("invalid_limit")
    return int(raw)


@dataclass(frozen=True)
class Config:
    products: frozenset[str]
    domains: frozenset[str]
    folder: str = field(repr=False)
    api_key: str = field(repr=False)
    max_results: int
    max_queries: int
    daily_limit: int
    ttl: int

    @classmethod
    def from_env(cls):
        return cls(
            products=_entries("YANDEX_SEARCH_PUBLIC_PRODUCTS", product_name),
            domains=_entries("YANDEX_SEARCH_ALLOWED_DOMAINS", domain_name),
            folder=os.getenv("YANDEX_SEARCH_FOLDER_ID", ""),
            api_key=os.getenv("YANDEX_SEARCH_API_KEY", ""),
            max_results=_number("MAX_RESULTS", 5, 100),
            max_queries=_number("MAX_QUERIES_PER_RUN", 5, 50),
            daily_limit=_number("DAILY_LIMIT", 20, 10_000),
            ttl=_number("CACHE_TTL", 86400, 2_592_000, minimum=0),
        )

    def validate_credentials(self):
        if not self.api_key or not self.folder:
            return "missing_credentials"
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", self.folder) or not re.fullmatch(
            r"[A-Za-z0-9_.~-]{1,512}", self.api_key
        ):
            return "invalid_configuration"
        return None
