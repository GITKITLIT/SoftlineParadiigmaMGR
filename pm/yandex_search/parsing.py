"""Decode Yandex XML and retain short plain-text evidence with provenance."""

import base64
import binascii
import hashlib
import json
import re
import unicodedata
from datetime import UTC, date, datetime
from xml.etree import ElementTree

from pm.search_security import source_url

from .client import MAX_HTTP_BYTES, SearchFailure

MAX_XML_BYTES = 1024 * 1024


def utc_stamp(value):
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def safe_xml(raw, limit=MAX_XML_BYTES):
    if len(raw) > limit:
        raise ValueError("xml_too_large")
    # Force UTF-8 before inspecting declarations, including documents with a BOM.
    text = raw.decode("utf-8-sig")
    if "\x00" in text or re.search(r"<!\s*(DOCTYPE|ENTITY)\b", text, re.I):
        raise ValueError("unsafe_xml")
    return ElementTree.fromstring(text)


def plain_text(value, limit):
    return " ".join(
        "".join(
            " " if unicodedata.category(char).startswith("C") else char
            for char in value
        ).split()
    )[:limit].strip()


def _field(node, path, limit):
    match = node.find(path)
    return plain_text("".join(match.itertext()), limit) if match is not None else ""


def _date_fields(doc):
    result = {"published_at": None}
    # A document's modtime is modification metadata, even when publication is unknown.
    for tag, key in (("published_at", "published"), ("modtime", "modified")):
        value = _field(doc, tag, 80)
        try:
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                result[key + "_date"] = date.fromisoformat(value).isoformat()
            elif value:
                timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if timestamp.utcoffset() is not None:
                    result[key + "_at"] = utc_stamp(timestamp)
        except (ValueError, OverflowError):
            pass
    return result


def _decode(payload):
    if len(payload) > MAX_HTTP_BYTES:
        raise SearchFailure("response_too_large")
    try:
        body = json.loads(payload)
        encoded = body["rawData"]
        if not isinstance(encoded, str):
            raise ValueError()
    except (ValueError, TypeError, KeyError, RecursionError):
        raise SearchFailure("invalid_response") from None
    if len(encoded) > 4 * ((MAX_XML_BYTES + 2) // 3):
        raise SearchFailure("response_too_large")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise SearchFailure("invalid_base64") from None
    if len(raw) > MAX_XML_BYTES:
        raise SearchFailure("response_too_large")
    try:
        root = safe_xml(raw)
    except (ValueError, ElementTree.ParseError):
        raise SearchFailure("invalid_xml") from None
    for node in root.iter():
        node.tag = node.tag.rsplit("}", 1)[-1]
    if root.tag != "yandexsearch":
        raise SearchFailure("invalid_xml")
    response = root.find("response")
    if response is None:
        raise SearchFailure("invalid_xml")
    return response


def parse_response(payload, *, domain, topic, query, product, retrieved_at, limit, secret):
    response = _decode(payload)
    errors = response.findall("error")
    if errors:
        codes = {node.get("code") for node in errors}
        if codes == {"15"}:
            return [], []
        if "32" in codes:
            raise SearchFailure("provider_daily_limit", "unavailable")
        raise SearchFailure("provider_xml_error")

    results = {}
    filtered = False
    for doc in response.iterfind("results/grouping/group/doc"):
        url = source_url(_field(doc, "url", 2049), domain)
        title = _field(doc, "title", 240)
        text = plain_text(" ".join(
            "".join(node.itertext()) for node in doc.findall("passages/passage")
        ), 1400)
        if not url or not text or any(secret in value for value in (url, title, text)):
            filtered = True
            continue
        key = "web:" + hashlib.sha256((url + "\0" + text).encode("utf-8")).hexdigest()[:32]
        results.setdefault(key, {
            "id": key, "kind": "competitor_web", "ref": url, "title": title or domain,
            "text": text, "url": url, "domain": domain, "topic": topic, "query": query,
            "product": product, "content_type": "snippet", "retrieved_at": retrieved_at,
            **_date_fields(doc),
        })
        if len(results) >= limit:
            break
    return list(results.values()), ["result_filtered"] if filtered else []
