"""Local retrieval, import of user-selected research ranges, constrained inference."""

import hashlib
import json
import os
import re
import time
import unicodedata
from collections import Counter
from math import log
from pathlib import Path
from urllib.request import ProxyHandler, Request, build_opener
from urllib.parse import urlparse
from openpyxl import load_workbook
from openpyxl.utils.cell import range_boundaries

from .settings import ROOT, private_dir

open_model = build_opener(ProxyHandler({})).open
OPTIONS = {
    "checkout": "Проверить, доходит ли выбранная лицензия до корзины.",
    "tracking": "Проверить, регистрируется ли событие при фактическом добавлении товара.",
    "channel": "Проверить, менялся ли канал покупки. Для этого нужны отдельные данные продаж через менеджеров.",
}


def reference_is_excluded(value):
    if not isinstance(value, str):
        return False
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "эталонныйкейс" in re.sub(r"[^а-яёa-z0-9]+", "", normalized)


def kind_is_supported(kind):
    return kind in ("Интервью", "Конкуренты", "Бэклог")


def range_excerpts(path, sheet, area, kind):
    if reference_is_excluded(sheet) or not kind_is_supported(kind):
        raise ValueError("Выберите исследовательский лист и тип источника")
    c1, r1, c2, r2 = range_boundaries(area)
    if None in (c1, r1, c2, r2) or (c2 - c1 + 1) * (r2 - r1 + 1) > 4000 or r2 > 10000:
        raise ValueError(
            "Укажите конечный диапазон до 4000 ячеек в первых 10000 строках"
        )
    book = load_workbook(path, read_only=True, data_only=True)
    doc = Path(path).stem
    try:
        items = []
        for row in book[sheet].iter_rows(
            min_col=c1, max_col=c2, min_row=r1, max_row=r2
        ):
            for cell in row:
                s = cell.value
                if isinstance(s, str) and s.strip():
                    ref = f"{doc} · {sheet}!{cell.coordinate}"
                    items.append(
                        dict(
                            id=hashlib.sha256((kind + "|" + ref).encode()).hexdigest()[:18],
                            ref=ref,
                            locator={"file": Path(path).name, "sheet": sheet, "cell": cell.coordinate},
                            kind=kind,
                            text=s.strip()[:2000],
                        )
                    )
    finally:
        book.close()
    return items


def index_range(path, sheet, area, kind, target=None):
    items = range_excerpts(path, sheet, area, kind)
    if target is None:
        from .ai_store import store_excerpts
        return store_excerpts(f"excel:{Path(path).name}:{sheet}:{area}:{kind}", items)
    target.parent.mkdir(exist_ok=True, parents=True)
    current = {
        x["id"]: x
        for x in (
            json.loads(target.read_text(encoding="utf8")) if target.exists() else []
        )
    }
    current.update((x["id"], x) for x in items)
    tmp = target.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(list(current.values()), ensure_ascii=False), encoding="utf8"
    )
    tmp.replace(target)
    return len(items)


def evidence(*, include_demo=True):
    external = os.getenv("PM_EVIDENCE")
    if not external and not include_demo:
        return []
    path = Path(external) if external else ROOT / "data/demo-evidence.json"
    return visible_excerpts(json.loads(path.read_text(encoding="utf-8-sig")))


def visible_excerpts(items):
    return [
        item for item in items
        if not reference_is_excluded(item.get("ref"))
        and not (isinstance(item.get("locator"), dict) and any(
            reference_is_excluded(item["locator"].get(key)) for key in ("file", "sheet")
        ))
    ]


def imported_sources():
    from .ai_store import stored_excerpts

    local = private_dir() / "sources.json"
    old = json.loads(local.read_text(encoding="utf8")) if local.exists() else []
    return visible_excerpts(list({item["id"]: item for item in old + stored_excerpts()}.values()))


def sources(*, include_demo=True):
    return evidence(include_demo=include_demo) + imported_sources()


def tokens(text):
    return {t[:5] for t in re.findall(r"[а-яёa-z0-9-]+", text.casefold()) if len(t) > 2}


def search(query, items):
    words = tokens(query) - tokens("почему какой какие что проверить показать оценить")
    if not words:
        return []
    related = (
        ("корзина", "checkout", "оформление", "покупка", "доставка"),
        ("лицензия", "license", "подписка", "тариф"),
        ("интервью", "респондент", "исследование", "custdev"),
        ("событие", "аналитика", "метрика", "цель", "конверсия"),
        ("заказ", "продажи", "orders", "выручка"),
    )
    expanded = set().union(*(tokens(" ".join(group)) for group in related
                             if words & tokens(" ".join(group)))) - words
    documents, occurrences = [], Counter()
    for i, item in enumerate(items):
        headline = tokens(item["ref"] + " " + item["kind"])
        content = tokens(item["text"])
        relevant = (words | expanded) & (content | headline)
        if relevant:
            occurrences.update(relevant)
            documents.append((i, item, headline, content, relevant))
    total = len(items)
    scored = []
    query_words = len(words)
    for i, item, headline, content, relevant in documents:
        score = sum(
            (2.5 if term in words else 0.75) * (1.35 if term in headline else 1) *
            log(1 + (total - occurrences[term] + 0.5) / (occurrences[term] + 0.5))
            for term in relevant
        )
        score += 1.5 * len(content & words) / query_words
        scored.append((score, i, item))
    return [item for _, _, item in sorted(scored, key=lambda row: (-row[0], row[1]))[:12]]


def analyze(query, overview, endpoint=None):
    started = time.perf_counter()
    corpus = sources()
    hits = search(query, corpus)
    release_terms = tokens(overview["series"][0]["name"]) | tokens(overview.get("release_id", ""))
    if hits and tokens(query) & (release_terms | tokens("корзина")):
        # Include the release-specific evidence from each stream even if vocabulary differs.
        found = {x["id"] for x in hits}
        hits.extend(
            x
            for x in corpus
            if x["id"] in {"sales", "funnel", "backlog", "interview", "competitor"}
            and x["id"] not in found
        )
    result = dict(
        mode="Расчёт и поиск", hits=hits, checks=["checkout", "tracking"], model=None
    )
    endpoint = endpoint or os.environ.get("PM_LLM_URL")
    if not hits:
        result["notice"] = "Подтверждающих фрагментов для запроса не найдено."
    elif endpoint:
        url = urlparse(endpoint)
        if (
            url.scheme != "http"
            or url.hostname not in ("localhost", "127.0.0.1", "::1")
            or url.username
            or url.password
        ):
            raise ValueError(
                "Для закрытых материалов разрешён только локальный адрес модели"
            )
        prompt_path = os.getenv("PM_LLM_PROMPT_FILE")
        if not prompt_path:
            result["notice"] = "Укажите путь к инструкции модели в PM_LLM_PROMPT_FILE."
            result["seconds"] = round(time.perf_counter() - started, 3)
            return result
        keys = [x["id"] for x in hits]
        schema = {
            "type": "object",
            "properties": {
                "check": {"type": "string", "enum": list(OPTIONS)},
                "question": {"type": "string", "maxLength": 240},
                "sources": {
                    "type": "array",
                    "items": {"type": "string", "enum": keys},
                    "minItems": 1,
                    "maxItems": 3,
                },
            },
            "required": ["check", "question", "sources"],
            "additionalProperties": False,
        }
        payload = dict(
            model="qwen3-4b",
            temperature=0,
            max_tokens=160,
            stream=False,
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "hypothesis", "strict": True, "schema": schema},
            },
            messages=[
                {
                    "role": "system",
                    "content": None,
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        dict(
                            question=query,
                            signal=overview["series"][0]["delta"],
                            hypotheses=OPTIONS,
                            evidence=[{k: x[k] for k in ("id", "text")} for x in hits],
                        ),
                        ensure_ascii=False,
                    ),
                },
            ],
        )
        try:
            prompt = Path(prompt_path).read_text(encoding="utf-8-sig").strip()
            if not prompt:
                raise ValueError("Пустая инструкция модели")
            payload["messages"][0]["content"] = prompt
            request = Request(
                endpoint.rstrip("/") + "/chat/completions",
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"},
            )
            with open_model(request, timeout=150) as response:
                raw = json.load(response)
            answer = json.loads(raw["choices"][0]["message"]["content"])
            chosen, refs = answer["check"], answer["sources"]
            if chosen not in OPTIONS or not refs or not set(refs) <= set(keys):
                raise ValueError("Некорректная ссылка модели")
            # Gate claims by source type as well as by ID.
            support = {
                "checkout": {"funnel", "backlog"},
                "tracking": {"funnel", "backlog"},
                "channel": {"interview", "sales"},
            }
            if not set(refs) & support[chosen]:
                raise ValueError("Нет соответствующего источника для гипотезы")
            question = answer.get("question", "").strip()
            indirect = re.fullmatch(
                r"Проверить,\s*(.{3,200}\bли\b[^.?!]{1,150})[.?!]?", question, re.I
            )
            if indirect:
                clause = indirect.group(1).strip()
                question = clause[0].upper() + clause[1:] + "?"
            if (
                not 8 <= len(question) <= 240
                or not question.endswith("?")
                or question.count("?") != 1
                or re.search(r"[0-9<>\r\n]", question)
            ):
                raise ValueError("Форма гипотезы не прошла проверку")
            anchors = {
                "checkout": ("корзин", "лиценз"),
                "tracking": ("событи", "регистр", "учёт", "учет", "метрик"),
                "channel": ("канал", "менеджер"),
            }
            if not any(word in question.casefold() for word in anchors[chosen]):
                raise ValueError("Вопрос расходится с выбранным способом проверки")
            result.update(
                mode="Локальная Qwen3-4B",
                model=raw.get("model", "qwen3-4b"),
                hypothesis=question,
                proposal=OPTIONS[chosen],
                refs=refs,
                usage=raw.get("usage", {}),
            )
        except (OSError, ValueError, KeyError, IndexError, TypeError, AttributeError):
            result["notice"] = (
                "Ответ модели недоступен или не прошёл проверку источников. Доступен расчёт и ручная проверка."
            )
    else:
        result["notice"] = (
            "Модель не подключена. Доступны рассчитанный сигнал и поиск источников."
        )
    result["seconds"] = round(time.perf_counter() - started, 3)
    return result
