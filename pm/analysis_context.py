"""Normalize bounded evidence for a single investigation."""
import hashlib
import json
import os
import re
from urllib.parse import urlsplit

from .analytics import day, weeks
from .research import search, sources
from .service import metrika

TOPICS = {"pricing", "licensing", "checkout", "product_features", "support"}
WEB_STATUSES = {"ok", "partial", "disabled", "unavailable", "error"}


def allowlist(name):
    try:
        items = json.loads(os.getenv(name, "[]"))
        if isinstance(items, list) and all(isinstance(x, str) and x for x in items):
            return set(items)
    except ValueError:
        pass
    return set()


def public_search(request, collector=None):
    if os.getenv("YANDEX_SEARCH_ENABLED", "").lower() != "true":
        return "disabled", "configuration_disabled", []
    if request is None:
        return "disabled", "no_public_scope", []
    if not isinstance(request, dict):
        return "error", "invalid_public_scope", []
    product, domains, topics = (request.get(k) for k in ("product", "domains", "topics"))
    allowed_domains = allowlist("YANDEX_SEARCH_ALLOWED_DOMAINS")
    allowed_products = allowlist("YANDEX_SEARCH_PUBLIC_PRODUCTS")
    valid = (
        isinstance(product, str) and product in allowed_products
        and isinstance(domains, list) and 0 < len(domains) <= 10
        and all(isinstance(x, str) and x in allowed_domains for x in domains)
        and isinstance(topics, list) and 0 < len(topics) <= len(TOPICS)
        and all(isinstance(x, str) and x in TOPICS for x in topics)
    )
    if not valid:
        return "error", "invalid_public_scope", []
    if collector is None:
        try:
            from .web_search import collect_competitor_evidence
        except ModuleNotFoundError as error:
            if error.name != "pm.web_search":
                return "unavailable", "adapter_import_failed", []
            return "disabled", "adapter_not_installed", []
        except ImportError:
            return "unavailable", "adapter_import_failed", []
        collector = collect_competitor_evidence
    try:
        response = collector(product=product, domains=list(dict.fromkeys(domains)),
                             topics=list(dict.fromkeys(topics)))
        status = response["status"]
        if response["schema_version"] != 1 or status not in WEB_STATUSES:
            raise ValueError
        items = [
            normalize_web(item, domains, product, topics)
            for item in response["items"][:10] if status in {"ok", "partial"}
        ]
        reason = response.get("reason")
        reason = reason if isinstance(reason, str) and re.fullmatch(r"[a-z_0-9]{1,64}", reason) else None
        normalized = [item for item in items if item]
        if len(normalized) != len(items):
            return "partial" if normalized else "error", "invalid_items", normalized
        return status, reason, normalized
    except Exception:
        return "error", "adapter_failed", []


def normalize_web(item, domains, product, topics):
    try:
        url = urlsplit(item["url"])
        if (
            url.scheme not in {"http", "https"} or url.hostname not in domains
            or url.username or url.password or item["domain"] != url.hostname
            or item["product"] != product or item["topic"] not in topics
            or item["content_type"] not in {"snippet", "page_excerpt"}
            or item["kind"] != "competitor_web"
            or not re.fullmatch(r"web:[\w.-]{1,100}", item["id"])
        ):
            return None
        return entry(item["id"], "competitor_web", item["title"], item["text"],
                     mode="public", quality=item["content_type"], locator=item["url"],
                     observed_at=item.get("published_at"), retrieved_at=item.get("retrieved_at"),
                     topic=item["topic"])
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def clean(value, size):
    return str(value).replace("\x00", "")[:size]


def entry(identifier, kind, ref, text="", *, mode, quality="observed", **extra):
    return dict(evidence_id=identifier, id=identifier, kind=kind, ref=clean(ref, 180),
                text=clean(text, 700), data_mode=mode, quality=quality, availability="available",
                unit=None, metric=None, filter=None, period=None, observed_at=None,
                retrieved_at=None, locator=None) | extra


def safe_locator(ref):
    # Legacy imports already store a workbook basename, new imports have structured coordinates.
    return re.sub(r"(?:[A-Za-z]:[\\/]|[/\\]{1,2})\S+", "[локальный путь]", clean(ref, 180))


def collect(query, snapshot, request, *, collector=None, meter=metrika):
    mode = "local" if os.getenv("PM_WORKBOOK") or os.getenv("PM_SNAPSHOT") else "synthetic"
    cfg = snapshot["config"]
    before, after = ((cfg[f"{key}_start"], cfg[f"{key}_end"]) for key in ("before", "after"))
    periods = {"before": before, "after": after}
    unequal = len(weeks(*map(day, before))) != len(weeks(*map(day, after)))
    limits = []
    if unequal:
        limits.append("Число недель в окнах отличается. Сравниваются средние, сезонность не учтена.")
    evidence = []
    for i, series in enumerate(snapshot["series"][:4]):
        values = {key: series.get(key) for key in ("before", "after", "delta", "rolling_base")}
        if values["before"] in (0, None) or values["after"] is None:
            limits.append("Для одной из групп относительное изменение недоступно.")
        evidence.append(entry(
            f"orders:{i}", "orders", series["name"], mode=mode, unit="index",
            metric=cfg["metric"], period=periods, filter=snapshot.get("channel"), values=values,
            quality="incomplete" if values["delta"] is None else "observed",
        ))
    if len(snapshot["series"]) < 2:
        limits.append("Контрольная группа отсутствует.")
    elif snapshot["series"][1].get("delta") is not None and snapshot["series"][0].get("delta") is not None:
        core, control = (item["delta"] for item in snapshot["series"][:2])
        if core < 0 and control >= 0:
            limits.append("Основная и контрольная группы движутся по-разному. Это не устанавливает причину.")
    for segment, values in list(snapshot.get("reach", {}).items())[:4]:
        numerator, denominator = values.get("num"), values.get("den")
        rate = 100 * numerator / denominator if numerator is not None and denominator else None
        evidence.append(entry(
            f"reach:{segment}", "funnel", "Выбор опции", mode=mode, unit="percent",
            metric="cfg_reach_rate", filter={"segment": segment},
            period={"start": cfg["reach_start"], "end": cfg["reach_end"]},
            quality="incomplete" if rate is None or values.get("days") != values.get("total") else "observed",
            values=values | {"rate": rate},
        ))
    evidence.append(entry("release:current", "release", snapshot.get("release_id") or "Релиз",
                          mode=mode, observed_at=snapshot.get("launch"),
                          values={"related_task": snapshot.get("release_task")}))
    backlog = snapshot.get("backlog") or {}
    matches = [backlog] if backlog.get("id") else []
    if matches:
        evidence.append(entry("backlog:related", "backlog", backlog["id"],
                              backlog.get("title", ""), mode=mode, values={"status": backlog.get("status")}))
        if backlog["id"] != snapshot.get("release_task"):
            limits.append("Найденная задача отличается от указанной связи релиза. Проверьте связь перед созданием дубля.")

    corpus = sources()
    if mode == "local" and not os.getenv("PM_EVIDENCE"):
        from .research import evidence as default_evidence
        demo_ids = {x["id"] for x in default_evidence()}
        corpus = [x for x in corpus if x.get("id") not in demo_ids]
    hits = search(query, corpus)
    web_status, web_reason, web = public_search(request.get("web"), collector)
    statuses = {
        "sales": {"status": "ok" if snapshot["series"] else "missing"},
        "research": {"status": "ok" if hits else "missing"},
        "release": {"status": "ok" if snapshot.get("launch") else "missing"},
        "backlog": {"status": "ok" if matches else "missing"},
        "competitor_web": {"status": web_status, "reason": web_reason},
        "metrika": {"status": "disabled", "reason": "not_requested"},
    }
    meter_request = request.get("metrika")
    if meter_request:
        if not isinstance(meter_request, dict):
            raise ValueError("Укажите параметры Метрики объектом")
        report = meter(meter_request["start"], meter_request["end"],
                       segment=meter_request.get("segment"), detailed=True)
        statuses["metrika"] = {"status": report.get("status", "missing"), "reason": report.get("reason")}
        for key, metric in report.get("metrics", {}).items():
            evidence.append(entry(
                f"metrika:{key}", "metrika", key, mode=report["scope"], unit=metric["unit"],
                metric=metric["metric"], period=report["period"], filter=report.get("segment"),
                retrieved_at=report.get("retrieved"), values={"value": metric["value"]},
                quality="sampled" if report.get("sampled") else "observed",
            ))
        if report.get("metrics"):
            limits.append("Счётчик Метрики рассматривается отдельно от заказов. Визиты не подтверждают изменение продаж.")
        if report.get("missing"):
            limits.append("Для выбранного счётчика отсутствует настроенная цель или e-commerce.")
    seen_ids = {x["id"] for x in evidence}
    seen_text = set()
    local_hits = []
    for item in hits:
        ref = safe_locator(item["ref"])
        kind = {"Интервью": "interview", "Конкуренты": "competitor_local",
                "Бэклог": "backlog"}.get(item["kind"], "research")
        identifier = "file:" + hashlib.sha256(str(item["id"]).encode()).hexdigest()[:18]
        provenance = item.get("locator")
        item_mode = "local" if (
            mode == "local" or provenance or str(item["id"]).startswith("u-") or os.getenv("PM_EVIDENCE")
        ) else "synthetic"
        locator = ref
        if isinstance(provenance, dict):
            locator = {key: safe_locator(provenance[key]) for key in ("file", "sheet", "cell", "paragraph")
                       if key in provenance}
        local_hits.append(entry(identifier, kind, ref, item["text"], mode=item_mode,
                                locator=locator, quality="excerpt"))
    for item in local_hits + web:
        normalized = " ".join(item["text"].casefold().split())
        if not normalized or normalized in seen_text or item["id"] in seen_ids:
            continue
        seen_text.add(normalized)
        seen_ids.add(item["id"])
        evidence.append(item)
    statuses["competitor_local"] = {"status": "ok" if any(x["kind"] == "competitor_local" for x in evidence) else "missing"}
    return mode, evidence[:24], statuses, limits, matches
