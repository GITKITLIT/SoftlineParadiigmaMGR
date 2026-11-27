"""Normalize bounded evidence for a single investigation."""
import hashlib
import json
import os
import re
from datetime import datetime, timedelta, timezone
from html import unescape
from statistics import mean
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
                     topic=item["topic"], product=product, competitor=host_from_url(item["url"]),
                     freshness=freshness(item.get("published_at")),
                     attributes=public_attributes(item), **verified_offer(item))
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def host_from_url(value):
    return urlsplit(value).hostname


OFFER_TERMS = ("product", "version", "license_type", "duration", "devices", "tax", "currency")


def public_attributes(item):
    attrs = item.get("attributes")
    if item.get("content_type") != "page_excerpt" or not isinstance(attrs, dict):
        return {}
    names = (*OFFER_TERMS, "price", "availability", "purchase_flow", "license_selection",
             "consultation", "payment", "price_valid_until")
    return {name: clean(attrs[name], 140) for name in names
            if isinstance(attrs.get(name), str) and attrs[name]}


def verified_offer(item):
    if item.get("content_type") != "page_excerpt":
        return {}
    offer = item.get("offer")
    if not isinstance(offer, dict):
        attrs = public_attributes(item)
        conflicts = item.get("attribute_conflicts") or {}
        if any(name in conflicts for name in (*OFFER_TERMS, "price")) or not all(attrs.get(key) for key in OFFER_TERMS):
            return {}
        price = attrs.get("price", "").replace(" ", "").replace(",", ".")
        currency = attrs["currency"].casefold()
        if not re.fullmatch(r"[0-9]{1,12}(?:[.][0-9]{1,4})?", price):
            return {}
        symbols = {"₽": "RUB", "руб": "RUB", "руб.": "RUB", "$": "USD", "€": "EUR"}
        terms = " | ".join(attrs[key].casefold() for key in OFFER_TERMS if key != "currency")
        if len(terms) > 350:
            return {}
        offer = {"metric": "price", "value": float(price), "currency": symbols.get(currency, currency.upper()),
                 "terms": terms}
    value, currency, terms = (offer.get(key) for key in ("value", "currency", "terms"))
    if (offer.get("metric") == "price" and type(value) in (int, float) and 0 <= value < 1e12
            and isinstance(currency, str) and re.fullmatch(r"[A-Z]{3}", currency)
            and isinstance(terms, str) and 0 < len(terms) <= 350):
        return {"offer": {"metric": "price", "value": value, "currency": currency, "terms": terms}}
    return {}


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


def freshness(value, today=None):
    today = today or datetime.now(timezone.utc).date()
    try:
        age_days = (today - day(value)).days
    except (TypeError, ValueError):
        return "unknown"
    return "future" if age_days < 0 else "stale" if age_days > 180 else "recent"


def web_text(value):
    if not isinstance(value, str):
        return ""
    text = unescape(value[:2500])
    text = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", " ", text, flags=re.I | re.S)
    return " ".join(re.sub(r"<[^>]{0,500}>", " ", text).split())


def clean(value, size):
    return str(value).replace("\x00", "")[:size]


def entry(identifier, kind, ref, text="", *, mode, quality="observed", **extra):
    from .memory import redact
    return dict(evidence_id=identifier, id=identifier, kind=kind, ref=redact(clean(ref, 180)),
                text=redact(clean(text, 700)), data_mode=mode, quality=quality, availability="available",
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
    "effect": "Согласованные критерии и последняя сохранённая оценка выбранной задачи",
    "compare_history": "Сравнение двух заданных периодов в одном историческом ряду",
    "search_history": "Релевантные исторические исследования, расследования и решения",
    "competitor_history": "История сопоставимых ценовых предложений по заданному периоду",
    "product_history": "Исторические выпуски и сравнение окон до и после выпуска",
}
FALLBACK = ("sales", "funnel", "releases", "backlog", "research", "competitors", "metrika", "public_competitors", "effect", "compare_history", "search_history", "competitor_history", "product_history")


EFFECT_TOOLS = {"plan": "sales", "review": "effect"}


class InvestigationContext:
    """Publish evidence only after its tool runs. Workbook tools share one snapshot."""

    def __init__(self, query, request, *, collector=None, meter=None, snapshot_loader=None, remember=False):
        self.query = query
        self.remember = remember
        self.request = request
        self.mode = "local" if os.getenv("PM_WORKBOOK") or os.getenv("PM_SNAPSHOT") else "synthetic"
        self.collector = collector
        self.meter = meter or metrika
        self.load_snapshot = snapshot_loader or (lambda: overview(for_analysis=True))
        self.snapshot = None
        self.corpus = None
        effect_intent = request.get("effect_intent")
        self.effect_tool = EFFECT_TOOLS.get(effect_intent) if isinstance(effect_intent, str) else None
        self.evidence = {}
        self.seen_text = set()
        self.limits = []
        self.matches = []
        self.completed = set()
        self.statuses = {key: {"status": "not_checked"} for key in (
            "sales", "funnel", "release", "backlog", "research", "competitor_local",
            "metrika", "competitor_web", "effect", "historical_metrics", "memory", "historical_competitors", "historical_releases",
        )}
        self.available = dict(TOOLS)
        scope = request.get("memory")
        if scope is not None and not isinstance(scope, dict):
            raise ValueError("Область исторического анализа должна быть объектом")
        self.memory_scope = scope or {}
        from .history import require_mode
        selected_mode = require_mode(self.memory_scope.get("mode", self.mode))
        if selected_mode != self.mode:
            self.mode = selected_mode
            for tool, status in (("sales", "sales"), ("funnel", "funnel"), ("releases", "release"),
                                 ("backlog", "backlog"), ("metrika", "metrika"), ("research", "research"),
                                 ("competitors", "competitor_local"), ("public_competitors", "competitor_web")):
                self.available.pop(tool)
                self.statuses[status] = {"status": "disabled", "reason": "different_current_data_mode"}
        if self.memory_scope.get("compare"):
            for tool, key in (("sales", "sales"), ("funnel", "funnel"),
                              ("releases", "release"), ("backlog", "backlog")):
                self.available.pop(tool, None)
                self.statuses[key] = {"status": "disabled", "reason": "historical_comparison_scope"}
        if not self.memory_scope.get("product"):
            self.available.pop("product_history")
            self.statuses["historical_releases"] = {"status": "disabled", "reason": "no_product_scope"}
        if not self.memory_scope.get("compare"):
            self.available.pop("compare_history")
            self.statuses["historical_metrics"] = {"status": "disabled", "reason": "no_period_selection"}
        if not self.memory_scope.get("competitors"):
            self.available.pop("competitor_history")
            self.statuses["historical_competitors"] = {"status": "disabled", "reason": "no_period_selection"}
        historical_words = ("год назад", "ранее", "истори", "архив", "прошл", "отверг", "квартал")
        if scope is None and not any(word in query.casefold() for word in historical_words):
            self.available.pop("search_history")
            self.statuses["memory"] = {"status": "disabled", "reason": "not_requested"}
        if not request.get("metrika"):
            self.available.pop("metrika", None)
            self.statuses["metrika"] = {"status": "disabled", "reason": "not_requested"}
        if not request.get("task_id"):
            self.available.pop("effect")
            self.statuses["effect"] = {"status": "disabled", "reason": "no_task_selected"}
        if not request.get("web") or os.getenv("YANDEX_SEARCH_ENABLED", "").lower() != "true":
            self.available.pop("public_competitors", None)
            self.statuses["competitor_web"] = {
                "status": "disabled",
                "reason": "no_public_scope" if not request.get("web") else "configuration_disabled",
            }

        if self.memory_scope.get("only_history") is True:
            for tool, key in (("sales", "sales"), ("funnel", "funnel"), ("releases", "release"),
                              ("backlog", "backlog"), ("research", "research"),
                              ("competitors", "competitor_local"), ("metrika", "metrika"),
                              ("public_competitors", "competitor_web")):
                if self.available.pop(tool, None):
                    self.statuses[key] = {"status": "disabled", "reason": "historical_chat_scope"}
        if self.effect_tool:
            self.available = {key: description for key, description in self.available.items()
                              if key == self.effect_tool}

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
            "effect": ("effect", self.effect),
            "compare_history": ("historical_metrics", self.compare_history),
            "search_history": ("memory", self.search_history),
            "competitor_history": ("historical_competitors", self.competitor_history),
            "product_history": ("historical_releases", self.product_history),
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
        if self.remember:
            from .collection import capture_sales
            capture_sales(snapshot, self.mode)
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
            planned = weeks(*map(day, before))[-4:]
            weekly_index = {day(point[0]): point[1] for point in series["points"]}
            previous = [weekly_index.get(date) for date in planned]
            baseline = mean(previous) if len(previous) == 4 and None not in previous else None
            baseline_window = [planned[0].isoformat(), planned[-1].isoformat()] if baseline is not None else None
            usable = values["delta"] is not None
            incomplete |= not usable
            self.add(entry(
                f"orders:{i}", "orders", series["name"], mode=self.mode, unit="index",
                metric=cfg["metric"], period=periods, filter=snapshot.get("channel"),
                values=values | {"weeks_before": counts["before"], "weeks_after": counts["after"],
                                 "alert_percent": cfg["alert_percent"], "baseline_4w": baseline,
                                 "baseline_window": baseline_window},
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
                f"reach:{segment}", "funnel", snapshot.get("reach_label", "Выбор опции"), mode=self.mode, unit="percent",
                metric="cfg_reach_rate", filter={"segment": segment},
                period={"start": cfg["reach_start"], "end": cfg["reach_end"]},
                quality="incomplete" if partial else "observed",
                values=values | {"rate": rate}, source_group="store_funnel",
            ))
        for segment, coverage in snapshot.get("basket_coverage", {}).items():
            incomplete |= coverage["days"] != coverage["total"]
            self.add(entry(
                f"basket:{segment}", "funnel", "Добавление в корзину", mode=self.mode, unit="percent",
                metric="cfg_add_to_cart_rate", filter={"segment": segment},
                period={"start": cfg["reach_start"], "end": cfg["reach_end"]},
                values={**coverage, "rate": None}, quality="incomplete", source_group="store_funnel",
                text=(f"{segment}: в окне нет наблюдений события для продукта." if not coverage["days"]
                      else f"{segment}: доступно покрытие события, конверсия по этому окну не рассчитана."),
            ))
            if not coverage["days"]:
                self.limits.append("Конверсия в корзину для выбранного продукта и устройства не измерена в выгрузке.")
        return ("missing", "no_segments") if not reach else ("partial", "incomplete_coverage") if incomplete else ("ok", None)

    def releases(self):
        snapshot = self.get_snapshot()
        current = snapshot.get("release_id")
        launch = snapshot.get("launch")
        if launch:
            self.add(entry("release:current", "release", current or "Релиз",
                           snapshot.get("release_title", ""), mode=self.mode, observed_at=launch, source_group="release_log",
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
        from .ai_store import task_list
        for task in task_list():
            if len(self.matches) >= 8:
                break
            if task["data_mode"] != self.mode or task["decision"] != "accept":
                continue
            if not terms & tokens(task["title"] + " " + task.get("verification", "")):
                continue
            item = {"id": task["id"], "title": task["title"], "status": "Согласована"}
            self.matches.append(item)
            self.add(entry(f"backlog:approved:{len(self.matches)}", "backlog", item["id"], item["title"],
                           mode=self.mode, values={"status": item["status"]}, source_group="backlog"))
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
                locator = {key: safe_locator(provenance[key]) for key in ("file", "sheet", "cell", "paragraph", "page", "slide", "part")
                           if key in provenance}
            group = locator.get("file", ref) if isinstance(locator, dict) else ref.split(" · ", 1)[0]
            self.add(entry(identifier, kind, ref, item["text"], mode="local" if private else self.mode,
                           locator=locator, quality="excerpt", source_group=f"document:{group}",
                           observed_at=item.get("observed_at"), freshness=freshness(item.get("observed_at"))))
        if hits and competitors and any(row.get("freshness") in {"stale", "unknown", "future"}
                                          for row in self.evidence.values() if row["kind"] == "competitor_local"):
            self.limits.append("Для части локальных сведений о конкурентах актуальность не подтверждена.")
        return ("ok", None) if hits else ("missing", "no_relevant_excerpt")

    def metrika(self):
        params = self.request["metrika"]
        if not isinstance(params, dict) or type(params.get("compare", False)) is not bool:
            return "error", "invalid_metric_scope"
        if params.get("compare"):
            cfg = self.get_snapshot()["config"]
            windows = [(period + ":", cfg[f"{period}_start"],
                        (day(cfg[f"{period}_end"]) + WEEK - timedelta(days=1)).isoformat())
                       for period in ("before", "after")]
        else:
            windows = [("", params["start"], params["end"])]
        statuses = []
        for prefix, start, end in windows:
            try:
                report = self.meter(start, end, segment=params.get("segment"), detailed=True)
            except (OSError, ValueError):
                statuses.append(("unavailable", "metric_request_failed"))
                continue
            labels = {"visits": "Визиты", "users": "Пользователи", "pageviews": "Просмотры",
                      "bounceRate": "Отказы", "avgVisitDurationSeconds": "Длительность визита"}
            for key, metric in report.get("metrics", {}).items():
                quality = "incomplete" if metric["value"] is None else "sampled" if report.get("sampled") else "observed"
                self.add(entry(
                    f"metrika:{prefix}{key}", "metrika", labels.get(key, key.replace("_", " ")), mode=report["scope"], unit=metric["unit"],
                    metric=metric["metric"], period=report["period"], filter=report.get("segment"),
                    retrieved_at=report.get("retrieved"), values={"value": metric["value"]},
                    quality=quality, source_group=f"metrika:{report['scope']}",
                ))
            statuses.append((report.get("status", "missing"), report.get("reason")))
        if any(key.startswith("metrika:") for key in self.evidence):
            self.limits.append("Визиты и цели Метрики измеряются отдельно от заказов. Совпадение изменений не устанавливает причину.")
        if len(set(statuses)) > 1:
            return "partial", "differing_window_status"
        return statuses[0]

    def public(self):
        status, reason, items = public_search(self.request["web"], self.collector)
        for item in items:
            self.add(item | {"source_group": f"public:{urlsplit(item['locator']).hostname}"})
        return status, reason


    def effect(self):
        from .ai_store import accepted_task

        task = accepted_task(self.request["task_id"])
        if not task:
            return "missing", "no_accepted_task"
        review = task.get("latest_review")
        plan = task["effect_plan"]
        values = {"baseline": plan["baseline"], "target": plan["target"], "plan": plan,
                  "status": review["status"] if review else "waiting_for_data",
                  "recommendation": review.get("recommendation") if review else "Дождаться четырёх полных недель."}
        if review:
            values.update(review.get("measurement", {}))
            for key in ("control_adjusted_change_pp", "change_from_baseline", "limitations", "external_factors"):
                if key in review:
                    values[key] = review[key]
        waiting = values["status"] == "waiting_for_data"
        self.add(entry("task:effect", "effect", task["title"], task["hypothesis"],
                       mode=task["data_mode"], values=values, source_group="effect_review",
                       quality="incomplete" if waiting else "observed"))
        return ("partial", "waiting_for_data") if waiting else ("ok", None)


    def compare_history(self):
        from .history import compare

        args = self.memory_scope["compare"] | {"mode": self.mode}
        result = compare(args)
        series = result["series"]
        for key in ("before", "after"):
            measured = result[key]
            self.add(entry(
                f"memory:metric:{series['id']}:{key}", "historical_metric", series["metric"],
                mode=self.mode, unit=series["unit"], period=measured["period"], quality=measured["status"],
                metric=series["metric"], source_group=f"history:{series['id']}",
                values={name: measured[name] for name in ("value", "days", "status")},
                locator={"series_id": series["id"], "observations": measured["observations"]},
            ))
        self.limits.extend(result["warnings"])
        if result["status"] != "ok":
            self.limits.append("Исторические окна неполны либо несопоставимы. Изменение по ним не рассчитано.")
            return "partial", "incomparable"
        # The two measured windows remain the primary records. The delta is derived.
        self.add(entry("memory:comparison", "historical_comparison", series["product"],
                       mode=self.mode, source_group=f"history:{series['id']}", quality="derived",
                       values={"delta": result["delta"], "delta_percent": result["delta_percent"]}))
        return "ok", None

    def search_history(self):
        from .history import search

        options = self.memory_scope.get("search", {})
        result = search(options | {"mode": self.mode, "query": self.query})
        for item in result["documents"][:4]:
            import json
            origin = json.loads(item["metadata"] or "{}").get("origin")
            self.add(entry(f"memory:document:{item['id']}", "historical_document", item["reference"],
                           item["content"], mode=self.mode, quality="reported" if origin == "user_reported" else "excerpt",
                           observed_at=item["observed"],
                           source_group=f"document:{item['reference'].split(' · ', 1)[0]}",
                           locator={"fragment_id": item["id"], "revision": item["revision"]}))
        for index, item in enumerate(result["decisions"][:4]):
            self.add(entry(f"memory:decision:{index}", "memory_context", "Предыдущее решение",
                           item.get("proposal") or item.get("summary") or "", mode=self.mode,
                           quality="historical_context", locator={"run_id": item["run_id"]},
                           observed_at=item["created"][:10],
                           values={"decision": item["decision"], "reason": item["decision_reason"],
                                   "effect": item["effect"]}))
        return ("ok", None) if result["documents"] or result["decisions"] else ("missing", "no_related_history")

    def competitor_history(self):
        from .history import competitor_changes

        rows = competitor_changes(self.memory_scope["competitors"] | {"mode": self.mode})
        for row in rows[:5]:
            self.add(entry(f"memory:offer:{row['observation_id']}", "historical_competitor", row["competitor"],
                           mode=self.mode, quality=row["quality"], locator=row["reference"],
                           observed_at=row["observed"], published_at=row["published"],
                           unit=row["currency"], terms=row["terms"],
                           source_group=f"public:{row['competitor']}", values={"before": row["before"], "after": row["after"]},
                           memory_observation_id=row["observation_id"]))
        return ("ok", None) if rows else ("missing", "no_comparable_price_changes")


    def product_history(self):
        from .history import product_history

        result = product_history(self.memory_scope["product"] | {"mode": self.mode})
        for row in result["releases"][:6]:
            numbers = row.get("comparison") or {}
            self.add(entry(f"memory:release:{row['observation_id']}", "historical_release", row["id"],
                           mode=self.mode, observed_at=row["date"], quality="observed",
                           source_group="release_log", memory_observation_id=row["observation_id"],
                           unit=numbers.get("unit"),
                           values={"before": numbers.get("before"), "after": numbers.get("after"),
                                   "delta": numbers.get("delta"), "interpretation": "descriptive_only"}))
        return ("ok", None) if result["releases"] else ("missing", "no_historical_releases")
