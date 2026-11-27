"""Bounded HTTPS transport. A caller reserves budget before each attempt."""

import http.client
import json
import random
import socket
import ssl
from contextlib import contextmanager
from threading import Lock, Timer
from time import monotonic, sleep
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

_HOST = "searchapi.api.cloud.yandex.net"
_PATH = "/v2/web/search"
MAX_HTTP_BYTES = 2 * 1024 * 1024
TIMEOUT_SECONDS = 5
_MAX_BACKOFF = 3
_SEARCH_GAP = 1.05
_RATE_LOCK = Lock()
_last_started = None


class SearchFailure(Exception):
    def __init__(self, reason, status="error", retry=False, retry_after=None):
        super().__init__(reason)
        self.reason = reason
        self.status = status
        self.retry = retry
        self.retry_after = retry_after


def _retry_after(value):
    if not value or len(value) > 80:
        return None
    if value.isascii() and value.isdigit():
        return int(value)
    try:
        return max(0, (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None


def retry_delay(failure, retry_index):
    # Preserve a longer provider cooldown by skipping the retry.
    if failure.retry_after is not None and failure.retry_after >= _MAX_BACKOFF:
        return None
    fallback = (1.0 if failure.reason == "rate_limited" else 0.25) * (2 ** retry_index)
    base = max(failure.retry_after or 0, fallback)
    return min(_MAX_BACKOFF, base + random.uniform(0, 0.2))


def _validate_status(response):
    status = response.status
    if status == 200:
        return
    if status == 429 or 500 <= status <= 599:
        raise SearchFailure(
            "rate_limited" if status == 429 else "provider_unavailable",
            "unavailable", retry=True, retry_after=_retry_after(response.getheader("Retry-After")),
        )
    if status in (401, 403):
        raise SearchFailure("authorization_failed", "unavailable")
    if 300 <= status < 400:
        raise SearchFailure("redirect_rejected")
    raise SearchFailure("request_rejected" if status == 400 else "unexpected_http_status")


def _pace_request():
    global _last_started
    with _RATE_LOCK:
        current = monotonic()
        if _last_started is not None:
            gap = _SEARCH_GAP - (current - _last_started)
            if gap > 0:
                sleep(gap)
        _last_started = monotonic()


@contextmanager
def response_deadline(connection, deadline):
    """Interrupt trickling headers and body at the deadline, keeping the socket pinned."""
    sock = connection.sock

    def interrupt():
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    timer = Timer(max(0, deadline - monotonic()), interrupt)
    timer.daemon = True
    timer.start()
    try:
        with connection.getresponse() as response:
            yield response
    finally:
        timer.cancel()


def bounded_body(response, limit, deadline):
    size = response.getheader("Content-Length")
    if size is not None:
        if len(size) > 10 or not size.isascii() or not size.isdigit():
            raise SearchFailure("invalid_response")
        size = int(size)
        if size > limit:
            raise SearchFailure("response_too_large")
    data = bytearray()
    while len(data) <= limit:
        if monotonic() >= deadline:
            raise TimeoutError()
        block = response.read1(min(64 * 1024, limit + 1 - len(data)))
        if not block:
            break
        data.extend(block)
    if len(data) > limit:
        raise SearchFailure("response_too_large")
    if monotonic() >= deadline:
        raise TimeoutError()
    if size is not None and size != len(data):
        raise SearchFailure("truncated_response", "unavailable", retry=True)
    return bytes(data)


def request_once(query, config):
    body = json.dumps({
        "query": {
            "searchType": "SEARCH_TYPE_RU", "queryText": query,
            "fixTypoMode": "FIX_TYPO_MODE_OFF",
        },
        "folderId": config.folder,
        "responseFormat": "FORMAT_XML",
        "groupSpec": {
            "groupMode": "GROUP_MODE_FLAT", "groupsOnPage": str(min(100, 2 * config.max_results)),
            "docsInGroup": "1",
        },
    }).encode("utf-8")
    headers = {"Authorization": "Api-Key " + config.api_key, "Content-Type": "application/json"}
    _pace_request()
    deadline = monotonic() + 2 * TIMEOUT_SECONDS
    try:
        # Direct TLS with the platform trust store. http.client does not follow redirects.
        conn = http.client.HTTPSConnection(_HOST, timeout=TIMEOUT_SECONDS)
        try:
            conn.request("POST", _PATH, body=body, headers=headers)
            with response_deadline(conn, deadline) as response:
                _validate_status(response)
                return bounded_body(response, MAX_HTTP_BYTES, deadline)
        finally:
            conn.close()
    except TimeoutError:
        raise SearchFailure("timeout", "unavailable", retry=True) from None
    except ssl.SSLError:
        raise SearchFailure("tls_failed", "unavailable") from None
    except (OSError, http.client.HTTPException):
        code = "timeout" if monotonic() >= deadline else "transport_failed"
        raise SearchFailure(code, "unavailable", retry=True) from None
