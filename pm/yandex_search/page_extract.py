"""Bounded static HTML extraction. Page content is untrusted evidence."""

import json
import re
from datetime import date
from html.parser import HTMLParser

from pm.competitor_sources import _read_metadata
from pm.search_security import TOPIC_SYNONYMS

from .client import SearchFailure
from .parsing import plain_text, relevance_score

_BLOCKS = {"p", "div", "h1", "h2", "h3", "h4", "li", "tr", "section", "article", "main",
           "summary", "dl", "dt", "dd", "title", "br"}
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param",
         "source", "track", "wbr"}
_SKIP = {"script", "style", "template", "noscript", "iframe", "svg", "nav", "footer", "aside",
         "form", "button"}
_INSTRUCTION = re.compile(
    r"(?:ignore|disregard|override).{0,60}(?:instructions|prompt|system)|"
    r"(?:игнорируй|игнорируйте|забудь|выполни).{0,60}(?:инструкц|промпт|команд)|"
    r"(?:system|assistant|developer)\s*:\s|<\|[^>]+\|>", re.I,
)
_CHALLENGE = re.compile(
    r"captcha|verify (?:that )?you are (?:a )?human|"
    r"подтвердите,? что вы (?:человек|не робот)|проверка (?:браузера|безопасности)", re.I,
)
_UX_FIELDS = {"license_selection", "purchase_flow", "self_checkout", "payment", "consultation",
              "support", "account", "renewal", "services", "assortment", "availability", "features"}
_HIDDEN_CLASSES = {"hidden", "sr-only", "visually-hidden", "d-none", "display-none"}


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.blocks = []
        self.current = []
        self.title = ""
        self.jsonld = []
        self.ld_chars = 0

    def _flush(self):
        if not self.current:
            return
        main, is_title, _ = self.stack[-1][2:] if self.stack else (False, False, False)
        text = plain_text("".join(self.current), 380)
        self.current.clear()
        if not text or _INSTRUCTION.search(text):
            return
        if is_title:
            self.title = self.title or text
        elif len(self.blocks) < 800:
            self.blocks.append((main, text))

    def handle_starttag(self, tag, raw_attrs):
        attrs = dict(raw_attrs)
        attrs = {key: value or "" for key, value in attrs.items()}
        _, parent_hidden, main, is_title, _ = self.stack[-1] if self.stack else (
            "", False, False, False, False,
        )
        if tag in _BLOCKS:
            self._flush()
        if tag in {"td", "th"} and self.current and not parent_hidden:
            self.current.append(" | ")
        if tag in _VOID:
            return
        if len(self.stack) >= 128:
            raise SearchFailure("page_structure_rejected")
        style = re.sub(r"\s+", "", attrs.get("style", "")).lower()
        hidden = (
            parent_hidden or tag in _SKIP or "hidden" in attrs or "inert" in attrs
            or attrs.get("aria-hidden", "").lower() == "true"
            or any(token in style for token in ("display:none", "visibility:hidden"))
            or bool(set(attrs.get("class", "").lower().split()) & _HIDDEN_CLASSES)
        )
        ld = tag == "script" and attrs.get("type", "").lower() == "application/ld+json" and not parent_hidden
        self.stack.append((tag, hidden, main or tag in {"article", "main"}, is_title or tag == "title", ld))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                if tag in _BLOCKS or any(frame[0] in _BLOCKS for frame in self.stack[index:]):
                    self._flush()
                del self.stack[index:]
                break

    def handle_data(self, data):
        hidden = self.stack[-1][1] if self.stack else False
        ld = self.stack[-1][4] if self.stack else False
        if ld and self.ld_chars + len(data) <= 65_536:
            self.jsonld.append(data)
            self.ld_chars += len(data)
        elif not hidden:
            self.current.append(data)

    def close(self):
        super().close()
        self._flush()


def _name_match(text, names):
    folded = " ".join(text.casefold().split())
    return any(re.search(r"(?<!\w)" + re.escape(name.casefold()) + r"(?!\w)", folded) for name in names)


def _schema_nodes(blobs):
    pending = []
    for raw in blobs:
        try:
            pending.append(json.loads(raw))
        except (ValueError, RecursionError):
            continue
    while pending:
        value = pending.pop()
        if isinstance(value, list):
            pending.extend(value[:100])
        elif isinstance(value, dict):
            yield value
            if "@graph" in value:
                pending.append(value["@graph"])


def _type(node, name):
    kind = node.get("@type", ())
    kinds = [kind] if isinstance(kind, str) else kind if isinstance(kind, list) else []
    return any(value in (name, f"https://schema.org/{name}", f"http://schema.org/{name}")
               for value in kinds if isinstance(value, str))


def _safe_scalar(value):
    if not isinstance(value, str) and type(value) not in {int, float}:
        return ""
    value = plain_text(str(value), 160)
    if _INSTRUCTION.search(value) or re.search(r"<[!/A-Za-z]", value):
        return ""
    return value


def _published_attributes(blobs, names):
    products = [node for node in _schema_nodes(blobs) if _type(node, "Product")
                and _name_match(_safe_scalar(node.get("name")), names)]
    if len(products) != 1:
        return {}
    item, = products
    labels = []
    offer = item.get("offers")
    if isinstance(offer, list) and len(offer) == 1:
        offer, = offer
    if isinstance(offer, dict) and _type(offer, "Offer"):
        availability = _safe_scalar(offer.get("availability")).rsplit("/", 1)[-1]
        labels.extend((
            ("цена", _safe_scalar(offer.get("price"))),
            ("валюта", _safe_scalar(offer.get("priceCurrency"))),
            ("наличие", availability if availability in {
                "InStock", "OutOfStock", "PreOrder", "LimitedAvailability", "SoldOut",
            } else ""),
        ))
    else:
        offer = {}
    for source in (item, offer):
        properties = source.get("additionalProperty")
        if isinstance(properties, list):
            for prop in properties[:40]:
                if isinstance(prop, dict) and _type(prop, "PropertyValue"):
                    labels.append((_safe_scalar(prop.get("name")), _safe_scalar(prop.get("value"))))
    labels.append(("продукт", _safe_scalar(item.get("name"))))
    attrs = _read_metadata(labels)
    expiry = _safe_scalar(offer.get("priceValidUntil"))
    if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", expiry):
        try:
            attrs["price_valid_until"] = date.fromisoformat(expiry).isoformat()
        except ValueError:
            pass
    return attrs


def _visible_attributes(blocks):
    pairs = []
    offers = []
    for block in blocks:
        labeled = [field.split(":", 1) for field in re.split(r"[;|]", block) if ":" in field]
        labeled = [(key.strip(), _safe_scalar(value)) for key, value in labeled]
        values = _read_metadata(labeled)
        if values.get("price"):
            offers.append(values)
        pairs.extend(labeled)
    result = {key: value for key, value in _read_metadata(pairs).items() if key in _UX_FIELDS}
    if len(offers) == 1:
        result = offers[0] | result
    return result


def extract_page(html, *, names, topic, secret):
    page = PageParser()
    page.feed(html)
    page.close()
    main = [text for is_main, text in page.blocks if is_main]
    blocks = main or [text for _, text in page.blocks]
    if _CHALLENGE.search(" ".join([page.title, *blocks[:5]])):
        raise SearchFailure("page_access_challenge")
    attributes = _published_attributes(page.jsonld, names)
    heading_matches = _name_match(page.title, names) or any(_name_match(text, names) for text in blocks[:8])
    ranked = sorted(
        dict.fromkeys(blocks),
        key=lambda text: relevance_score("", text, names, topic), reverse=True,
    )
    topics = tuple(word[:5] for words in TOPIC_SYNONYMS.values() for word in " ".join(words).split()
                   if len(word) >= 4)
    selected = [
        text for text in ranked if (len(text) >= 18 and secret not in text)
        and (_name_match(text, names) or ((attributes or heading_matches)
             and any(word in text.casefold() for word in topics)))
    ][:4]
    visible = _visible_attributes(selected)
    conflicts = {key: [value, visible[key]] for key, value in attributes.items()
                 if key in visible and value.casefold() != visible[key].casefold()}
    safe_attributes = {key: value for key, value in (visible | attributes).items()
                       if secret not in value and key not in conflicts}
    conflicts = {key: values for key, values in conflicts.items() if all(secret not in v for v in values)}
    origins = {key: "jsonld" if key in attributes else "visible" for key in safe_attributes}
    if not (safe_attributes or selected or conflicts):
        raise SearchFailure("page_no_evidence")
    title = page.title if secret not in page.title else ""
    return {
        "title": title, "fragments": selected, "attributes": safe_attributes,
        "attribute_sources": origins, "attribute_conflicts": conflicts,
    }
