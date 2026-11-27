"""Public synchronous v1 adapter for competitive search evidence."""

import sqlite3
import time
from contextlib import nullcontext

from .search_security import public_queries
from .yandex_search.client import SearchFailure, request_once, retry_delay
from .yandex_search.config import Config, enabled
from .yandex_search.page_policy import PageConfig
from .yandex_search.parsing import parse_response, utc_stamp
from .yandex_search.storage import Store, cache_key, now_utc

_STORAGE_ERRORS = (OSError, sqlite3.Error, ValueError, RecursionError)
_MAX_ATTEMPTS = 3
_STOP_LIVE = {
    "authorization_failed", "tls_failed", "redirect_rejected",
    "provider_daily_limit", "provider_not_configured", "daily_limit", "rate_limited",
    "storage_unavailable",
}


def _envelope():
    return {
        "schema_version": 1, "status": "ok", "reason": None, "source": "yandex_search",
        "retrieved_at": utc_stamp(now_utc()), "items": [], "warnings": [],
        "usage": {"requests": 0, "cache_hits": 0},
    }


def _search(store, config, result, *, domain, topic, query, product, smoke):
    usage = result["usage"]
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
        started = time.perf_counter()
        attempt_info = {"domain": domain, "topic": topic}
        result["attempts"].append(attempt_info)
        try:
            payload = request_once(query, config)
            fetched = now_utc()
            items, warnings = parse_response(
                payload, domain=domain, topic=topic, query=query, product=product,
                retrieved_at=utc_stamp(fetched), limit=config.max_results, secret=config.api_key,
                variants=config.aliases.get(product, ()), diagnostics=attempt_info,
            )
            attempt_info["outcome"] = "ok"
            return items, warnings, fetched
        except SearchFailure as failure:
            attempt_info["outcome"] = failure.reason
            has_budget = usage["requests"] < config.max_queries
            if smoke or not failure.retry or not has_budget or attempt + 1 == _MAX_ATTEMPTS:
                raise
            delay = retry_delay(failure, attempt)
            if delay is None:
                raise
        finally:
            attempt_info["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 1)
        time.sleep(delay)


def _resolve_query(store, config, result, product, query_info, stop_live, smoke):
    domain, topic, query = query_info
    use_cache = not smoke and config.ttl > 0
    key = cache_key(config.folder, query, config.max_results)
    with store.key_lock(key) if use_cache else nullcontext():
        if use_cache:
            result["metrics"]["cache_lookups"] += 1
            cached, expired = store.get(key, now_utc(), config.ttl)
            if expired:
                result["metrics"]["expired_cache_entries"] += 1
                result["warnings"].append("expired_cache")
            if cached is not None:
                result["usage"]["cache_hits"] += 1
                return cached
        if stop_live is not None:
            raise stop_live
        items, notes, fetched = _search(
            store, config, result, domain=domain, topic=topic, query=query,
            product=product, smoke=smoke,
        )
        if use_cache:
            try:
                store.put(key, items, notes, fetched, config.ttl)
            except _STORAGE_ERRORS:
                result["warnings"].append("cache_write_failed")
        return items, notes


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
        product, queries = public_queries(
            product, domains, topics, config.products, config.domains, config.aliases,
        )
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
    found = {}
    coverage = result["coverage"]
    successes = 0
    stop_live = None
    for query_info in queries[:config.max_queries]:
        domain, topic, query = query_info
        check = {"domain": domain, "topic": topic, "count": 0}
        coverage.append(check)
        try:
            items, notes = _resolve_query(store, config, result, product, query_info, stop_live, smoke)
        except SearchFailure as failure:
            failures.append(failure)
            check.update(status=failure.status, reason=failure.reason)
            if failure.reason in _STOP_LIVE:
                stop_live = failure
            continue
        except _STORAGE_ERRORS:
            failures.append(SearchFailure("storage_unavailable", "unavailable"))
            check.update(status="unavailable", reason="storage_unavailable")
            continue

        successes += 1
        check.update(status="ok", reason=None, count=len(items))
        warnings.update(dict.fromkeys(notes))
        for item in items:
            match = {"topic": topic, "query": query, "retrieved_at": item["retrieved_at"]}
            previous = found.setdefault(item["id"], item | {"matches": []})
            previous["matches"].append(match)

    if len(queries) > config.max_queries:
        failures.append(SearchFailure("run_limit", "unavailable"))
        coverage.extend(
            {"domain": domain, "topic": topic, "status": "skipped", "reason": "run_limit", "count": 0}
            for domain, topic, _ in queries[config.max_queries:]
        )
    if failures:
        first = failures[0]
        result.update(status="partial" if successes else first.status, reason=first.reason)
        warnings.update(dict.fromkeys(failure.reason for failure in failures))
    warnings.update(dict.fromkeys(result["warnings"]))
    ranked = sorted(found.values(), key=lambda item: item.get("relevance", 0), reverse=True)
    result.update(items=ranked, warnings=list(warnings))
    if not smoke:
        try:
            page_config = PageConfig.from_env(config.domains)
        except (ValueError, RecursionError):
            result["warnings"].append("invalid_page_configuration")
        else:
            if page_config:
                from .yandex_search.pages import augment_pages
                augment_pages(result, page_config=page_config, search_config=config, store=store)
    return result


def _collect(*, product, domains, topics, smoke=False):
    start = time.perf_counter()
    result = _envelope()
    metrics = {"cache_lookups": 0, "cache_hit_rate": None, "expired_cache_entries": 0}
    result.update(metrics=metrics, coverage=[], attempts=[])
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
