"""Public synchronous v1 adapter for competitive search evidence."""

import sqlite3
import time

from .search_security import public_queries
from .yandex_search.client import SearchFailure, request_once, retry_delay
from .yandex_search.config import Config, enabled
from .yandex_search.parsing import parse_response, utc_stamp
from .yandex_search.storage import Store, cache_key, now_utc

_STORAGE_ERRORS = (OSError, sqlite3.Error, ValueError, RecursionError)
_MAX_ATTEMPTS = 3
_STOP_LIVE = {
    "authorization_failed", "tls_failed", "redirect_rejected",
    "provider_daily_limit", "daily_limit", "rate_limited", "storage_unavailable",
}


def _envelope():
    return {
        "schema_version": 1, "status": "ok", "reason": None, "source": "yandex_search",
        "retrieved_at": utc_stamp(now_utc()), "items": [], "warnings": [],
        "usage": {"requests": 0, "cache_hits": 0},
    }


def _search(store, config, usage, *, domain, topic, query, product, smoke):
    for attempt in range(1 if smoke else _MAX_ATTEMPTS):
        if usage["requests"] >= config.max_queries:
            raise SearchFailure("run_limit", "unavailable")
        try:
            reserved = store.reserve(now_utc(), config.daily_limit)
        except _STORAGE_ERRORS:
            raise SearchFailure("storage_unavailable", "unavailable") from None
        if not reserved:
            raise SearchFailure("daily_limit", "unavailable")
        usage["requests"] += 1
        try:
            payload = request_once(query, config)
            fetched = now_utc()
            items, warnings = parse_response(
                payload, domain=domain, topic=topic, query=query, product=product,
                retrieved_at=utc_stamp(fetched), limit=config.max_results, secret=config.api_key,
            )
            return items, warnings, fetched
        except SearchFailure as failure:
            has_budget = usage["requests"] < config.max_queries
            if smoke or not failure.retry or not has_budget or attempt + 1 == _MAX_ATTEMPTS:
                raise
            delay = retry_delay(failure, attempt)
            if delay is None:
                raise
            time.sleep(delay)


def _run(result, *, product, domains, topics, smoke):
    try:
        if not enabled("YANDEX_SEARCH_ENABLED"):
            return result | {"status": "disabled", "reason": "search_disabled"}
        if smoke and not enabled("YANDEX_SEARCH_LIVE_SMOKE"):
            return result | {"status": "disabled", "reason": "live_smoke_disabled"}
        config = Config.from_env()
    except (ValueError, RecursionError):
        return result | {"status": "disabled", "reason": "invalid_configuration"}
    if not config.products or not config.domains:
        return result | {"status": "disabled", "reason": "allowlist_not_configured"}
    try:
        product, queries = public_queries(product, domains, topics, config.products, config.domains)
    except ValueError:
        return result | {"status": "error", "reason": "invalid_input"}
    credential_error = config.validate_credentials()
    if credential_error:
        return result | {"status": "disabled", "reason": credential_error}
    if smoke and len(queries) != 1:
        return result | {"status": "error", "reason": "smoke_requires_one_query"}
    try:
        store = Store()
    except _STORAGE_ERRORS:
        return result | {"status": "unavailable", "reason": "storage_unavailable"}

    failures = []
    warnings = {}
    usage = result["usage"]
    found = {}
    successes = 0
    stop_live = None
    for domain, topic, query in queries[:config.max_queries]:
        key = cache_key(config.folder, query, config.max_results)
        try:
            cached = None
            if not smoke and config.ttl:
                result["metrics"]["cache_lookups"] += 1
                cached = store.get(key, now_utc(), config.ttl)
            if cached is not None:
                usage["cache_hits"] += 1
                items, notes = cached
            else:
                if stop_live is not None:
                    raise stop_live
                items, notes, fetched = _search(
                    store, config, usage, domain=domain, topic=topic, query=query,
                    product=product, smoke=smoke,
                )
                try:
                    if not smoke:
                        store.put(key, items, notes, fetched, config.ttl)
                except _STORAGE_ERRORS:
                    warnings["cache_write_failed"] = None
        except SearchFailure as failure:
            failures.append(failure)
            if failure.reason in _STOP_LIVE:
                stop_live = failure
            continue
        except _STORAGE_ERRORS:
            failures.append(SearchFailure("storage_unavailable", "unavailable"))
            continue

        successes += 1
        warnings.update(dict.fromkeys(notes))
        for item in items:
            match = {"topic": topic, "query": query, "retrieved_at": item["retrieved_at"]}
            previous = found.setdefault(item["id"], item | {"matches": []})
            previous["matches"].append(match)

    if len(queries) > config.max_queries:
        failures.append(SearchFailure("run_limit", "unavailable"))
    if failures:
        first = failures[0]
        result.update(status="partial" if successes else first.status, reason=first.reason)
        warnings.update(dict.fromkeys(failure.reason for failure in failures))
    result.update(items=list(found.values()), warnings=list(warnings))
    return result


def _collect(*, product, domains, topics, smoke=False):
    start = time.perf_counter()
    result = _envelope()
    metrics = {"cache_lookups": 0, "cache_hit_rate": None}
    result["metrics"] = metrics
    result = _run(result, product=product, domains=domains, topics=topics, smoke=smoke)
    if metrics["cache_lookups"]:
        metrics["cache_hit_rate"] = round(
            result["usage"]["cache_hits"] / metrics["cache_lookups"], 4,
        )
    metrics["elapsed_ms"] = round((time.perf_counter() - start) * 1000, 1)
    return result


def collect_competitor_evidence(*, product: str, domains: list[str], topics: list[str]) -> dict:
    """Collect normalized evidence from allowlisted public search inputs."""
    return _collect(product=product, domains=domains, topics=topics)
