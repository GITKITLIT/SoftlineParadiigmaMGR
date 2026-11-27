"""Normalize bounded evidence for a single investigation."""
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from html import unescape
from urllib.parse import urlsplit

from .analytics import WEEK, day, weeks
from .research import search, sources, tokens
from .service import metrika, overview, recalculate

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


def allowed_domain(value):
    if not isinstance(value, str) or len(value) > 253:
        return False
    labels = value.split(".")
    return (len(labels) >= 2 and labels[-1] not in {"internal", "local", "localhost", "test", "invalid"}
            and not labels[-1].isdigit()
            and all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", name) for name in labels))


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
        and all(allowed_domain(x) and x in allowed_domains for x in domains)
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
        if response.get("source") == "synthetic_fixture":
            for item in normalized:
                item["data_mode"] = "synthetic"
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
        title = web_text(item["title"])
        text = web_text(item["text"])
        if not title or not text:
            return None
        return entry(item["id"], "competitor_web", title, text,
                     mode="public", quality=item["content_type"], locator=item["url"],
                     observed_at=utc(item.get("published_at")), retrieved_at=utc(item.get("retrieved_at")),
                     topic=item["topic"])
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def utc(value):
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            return parsed.astimezone(timezone.utc).isoformat()
    except ValueError:
        pass
    return None


def web_text(value):
    if not isinstance(value, str):
        return ""
    text = unescape(value[:2500])
    text = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", " ", text, flags=re.I | re.S)
    return " ".join(re.sub(r"<[^>]{0,500}>", " ", text).split())


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



TOOLS = {
    "sales": "Средние индексы заказов, аномалия по порогу, контроль и полнота недель",
    "funnel": "Достижение шага воронки по сегментам и покрытие дней в локальной выгрузке",
    "metrika": "Показатели и цели Яндекс.Метрики по выбранному оператором периоду",
    "releases": "Релизы в выбранных периодах и их связь с задачами",
    "research": "Фрагменты интервью, CustDev и других пользовательских исследований",
    "competitors": "Фрагменты локальных конкурентных исследований",
    "backlog": "Связанная задача и похожие записи из существующего бэклога",
    "public_competitors": "Конкуренты из явно выбранного публичного каталога",
}
FALLBACK = ("sales", "funnel", "releases", "backlog", "research", "competitors", "metrika", "public_competitors")


class InvestigationContext:
    """Per-run evidence ledger. No source is read before its tool is invoked."""

    def __init__(self, query, request, *, collector=None, meter=None, snapshot_loader=None):
        self.query = query
        self.request = request
        self.mode = "local" if os.getenv("PM_WORKBOOK") or os.getenv("PM_SNAPSHOT") else "synthetic"
        self.collector = collector
        self.meter = meter or metrika
        self.load_snapshot = snapshot_loader or (lambda: overview(for_analysis=True))
        self.snapshot = None
        self.corpus = None
        self.evidence = {}
        self.seen_text = set()
        self.limits = []
        self.matches = []
        self.completed = set()
        self.statuses = {key: {"status": "not_checked"} for key in (
            "sales", "funnel", "release", "backlog", "research", "competitor_local",
            "metrika", "competitor_web",
        )}
        self.available = dict(TOOLS)
        if not request.get("metrika"):
            self.available.pop("metrika")
            self.statuses["metrika"] = {"status": "disabled", "reason": "not_requested"}
        if not request.get("web") or os.getenv("YANDEX_SEARCH_ENABLED", "").lower() != "true":
            self.available.pop("public_competitors")
            self.statuses["competitor_web"] = {
                "status": "disabled",
                "reason": "no_public_scope" if not request.get("web") else "configuration_disabled",
            }

    def get_snapshot(self):
        if self.snapshot is None:
            self.snapshot = recalculate(self.load_snapshot(), self.request.get("config", {}), allow_partial=True)
        return self.snapshot

    def add(self, item):
        text = " ".join(item["text"].casefold().split())
        if item["id"] in self.evidence or (text and text in self.seen_text):
            return
        if text:
            self.seen_text.add(text)
        self.evidence[item["id"]] = item

    def run(self, name, arguments):
        if name not in self.available or name in self.completed:
            raise ValueError("Инструмент недоступен или уже вызван")
        if arguments != {}:
            raise ValueError("Инструменты принимают только параметры утверждённого запроса")
        self.completed.add(name)
        handlers = {
            "sales": ("sales", self.sales),
            "funnel": ("funnel", self.funnel),
            "metrika": ("metrika", self.metrika),
            "releases": ("release", self.releases),
            "research": ("research", lambda: self.research(False)),
            "competitors": ("competitor_local", lambda: self.research(True)),
            "backlog": ("backlog", self.backlog),
            "public_competitors": ("competitor_web", self.public),
        }
        key, handler = handlers[name]
        before = set(self.evidence)
        try:
            status, reason = handler()
        except TimeoutError:
            status, reason = "unavailable", "timeout"
        except (OSError, KeyError, IndexError, TypeError, AttributeError, ValueError):
            status, reason = "error", "invalid_or_unavailable_input"
        current = {"status": status}
        if reason:
            current["reason"] = reason
        self.statuses[key] = current
        return current | {"evidence_ids": [ref for ref in self.evidence if ref not in before]}

    def sales(self):
        snapshot = self.get_snapshot()
        cfg = snapshot["config"]
        before, after = ((cfg[f"{key}_start"], cfg[f"{key}_end"]) for key in ("before", "after"))
        periods = {"before": before, "after": after}
        counts = {key: len(weeks(*map(day, dates))) for key, dates in periods.items()}
        if counts["before"] != counts["after"]:
            self.limits.append("Длина окон отличается. Сравнение средних не учитывает сезонность.")
        series_rows = snapshot["series"][:4]
        incomplete = False
        for i, series in enumerate(series_rows):
            values = {key: series.get(key) for key in ("before", "after", "delta", "rolling_base")}
            usable = values["delta"] is not None
            incomplete |= not usable
            self.add(entry(
                f"orders:{i}", "orders", series["name"], mode=self.mode, unit="index",
                metric=cfg["metric"], period=periods, filter=snapshot.get("channel"),
                values=values | {"weeks_before": counts["before"], "weeks_after": counts["after"],
                                 "alert_percent": cfg["alert_percent"]},
                quality="observed" if usable else "incomplete",
                source_group="orders_series",
            ))
        if not series_rows:
            return "missing", "no_series"
        if len(series_rows) < 2:
            self.limits.append("Контрольная группа отсутствует.")
        if incomplete:
            self.limits.append("Относительное изменение недоступно для ряда с пропусками или нулевой базой.")
        has_missing = any(row["before"] is None or row["after"] is None for row in series_rows)
        return ("partial", "missing_weeks" if has_missing else "zero_baseline") if incomplete else ("ok", None)

    def funnel(self):
        snapshot = self.get_snapshot()
        cfg = snapshot["config"]
        reach = snapshot.get("reach", {})
        incomplete = False
        for segment, values in list(reach.items())[:4]:
            num, den = values.get("num"), values.get("den")
            rate = 100 * num / den if num is not None and den else None
            partial = rate is None or values.get("days") != values.get("total")
            incomplete |= partial
            self.add(entry(
                f"reach:{segment}", "funnel", "Выбор опции", mode=self.mode, unit="percent",
                metric="cfg_reach_rate", filter={"segment": segment},
                period={"start": cfg["reach_start"], "end": cfg["reach_end"]},
                quality="incomplete" if partial else "observed",
                values=values | {"rate": rate}, source_group="store_funnel",
            ))
        return ("missing", "no_segments") if not reach else ("partial", "incomplete_coverage") if incomplete else ("ok", None)

    def releases(self):
        snapshot = self.get_snapshot()
        current = snapshot.get("release_id")
        launch = snapshot.get("launch")
        if launch:
            self.add(entry("release:current", "release", current or "Релиз",
                           mode=self.mode, observed_at=launch, source_group="release_log",
                           values={"related_task": snapshot.get("release_task")}))
        cfg = snapshot["config"]
        begin, end = day(cfg["before_start"]), day(cfg["after_end"])
        release_ids = set()
        for row in snapshot.get("release_history", [])[:12]:
            try:
                released = day(row["date"])
            except (ValueError, KeyError, TypeError):
                continue
            row_id = row.get("id")
            if begin <= released < end + WEEK and row_id and row_id not in release_ids and row_id != current:
                release_ids.add(row_id)
                self.add(entry(f"release:nearby:{len(release_ids)}", "release", row_id,
                               row.get("title", ""), mode=self.mode, observed_at=released.isoformat(),
                               values={"related_task": row.get("related_task")}, source_group="release_log"))
            if len(release_ids) == 3:
                break
        if release_ids:
            self.limits.append("В сравниваемых окнах есть другие релизы. Их влияние требует отдельной проверки.")
        return ("ok", None) if launch or release_ids else ("missing", "no_release")

    def backlog(self):
        snapshot = self.get_snapshot()
        backlog = snapshot.get("backlog") or {}
        if backlog.get("id"):
            self.add(entry("backlog:related", "backlog", backlog["id"], backlog.get("title", ""),
                           mode=self.mode, values={"status": backlog.get("status")}, source_group="backlog"))
            self.matches.append(backlog)
            if backlog["id"] != snapshot.get("release_task"):
                self.limits.append("Задача отличается от указанной связи релиза. Проверьте связь перед созданием дубля.")
        terms = tokens(self.query)
        for row in snapshot.get("backlog_history", [])[:200]:
            title = row.get("title", "")
            if row.get("id") and terms & tokens(title) and not any(x["id"] == row["id"] for x in self.matches):
                self.matches.append({key: row.get(key) for key in ("id", "title", "status")})
                self.add(entry(f"backlog:match:{len(self.matches)}", "backlog", row["id"], title,
                               mode=self.mode, values={"status": row.get("status")}, source_group="backlog"))
            if len(self.matches) >= 4:
                break
        return ("ok", None) if self.matches else ("missing", "no_matching_task")

    def research(self, competitors):
        if self.corpus is None:
            if self.mode == "local" and not os.getenv("PM_EVIDENCE"):
                from .research import imported_sources
                self.corpus = imported_sources()
            else:
                self.corpus = sources()
        items = [item for item in self.corpus if (item["kind"] == "Конкуренты") == competitors]
        hits = search(self.query, items)[:6]
        for item in hits:
            ref = safe_locator(item["ref"])
            kind = {"Интервью": "interview", "Конкуренты": "competitor_local",
                    "Бэклог": "backlog"}.get(item["kind"], "research")
            identifier = "file:" + hashlib.sha256(str(item["id"]).encode()).hexdigest()[:18]
            provenance = item.get("locator")
            private = provenance or str(item["id"]).startswith("u-") or os.getenv("PM_EVIDENCE")
            locator = ref
            if isinstance(provenance, dict):
                locator = {key: safe_locator(provenance[key]) for key in ("file", "sheet", "cell", "paragraph")
                           if key in provenance}
            group = locator.get("file", ref) if isinstance(locator, dict) else ref.split(" · ", 1)[0]
            self.add(entry(identifier, kind, ref, item["text"], mode="local" if private else self.mode,
                           locator=locator, quality="excerpt", source_group=f"document:{group}",
                           observed_at=item.get("observed_at")))
        return ("ok", None) if hits else ("missing", "no_relevant_excerpt")

    def metrika(self):
        params = self.request["metrika"]
        if not isinstance(params, dict):
            return "error", "invalid_metric_scope"
        report = self.meter(params["start"], params["end"], segment=params.get("segment"), detailed=True)
        for key, metric in report.get("metrics", {}).items():
            quality = "incomplete" if metric["value"] is None else "sampled" if report.get("sampled") else "observed"
            self.add(entry(
                f"metrika:{key}", "metrika", key, mode=report["scope"], unit=metric["unit"],
                metric=metric["metric"], period=report["period"], filter=report.get("segment"),
                retrieved_at=report.get("retrieved"), values={"value": metric["value"]},
                quality=quality, source_group=f"metrika:{report['scope']}",
            ))
        if report.get("metrics"):
            self.limits.append("Счётчик Метрики рассматривается отдельно от заказов. Визиты не подтверждают изменение продаж.")
        if report.get("missing"):
            self.limits.append("Для счётчика отсутствует настроенная цель или e-commerce.")
        return report.get("status", "missing"), report.get("reason")

    def public(self):
        status, reason, items = public_search(self.request["web"], self.collector)
        for item in items:
            self.add(item | {"source_group": f"public:{urlsplit(item['locator']).hostname}"})
        return status, reason
