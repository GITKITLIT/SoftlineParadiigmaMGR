"use strict";
const boot = JSON.parse(document.getElementById("boot").textContent);
let data = boot.data,
  observation = null,
  assessment = null,
  proposal = "",
  proof = [];
const $ = (s) => document.querySelector(s),
  fmt = (n, d = 2) =>
    n == null
      ? "нет базы"
      : n.toLocaleString("ru-RU", {
          minimumFractionDigits: d,
          maximumFractionDigits: d,
        });
const pct = (n) =>
  n == null
    ? "нет базы"
    : (n > 0 ? "+" : "") +
      fmt(Math.abs(n) < 0.05 ? 0 : n, Math.abs(n) < 0.05 ? 0 : 1) +
      "%";
const key =
  "paradiigma.task.v1:" +
  JSON.stringify([data.release_id, data.launch, data.series[0].name]);
const offline = location.protocol === "file:";
// Translate rendering only. Keep wire values and element identifiers intact.
const publicCopy = (value) => typeof value !== "string" ? value : value
  .replace(/Модель не подключена или ответ не прошёл проверку\. Показан расчёт и поиск\./gi, "Доступен аналитический режим")
  .replace(/(?:локальная\s+)?\bqwen(?:[\w.-]*[\w])?\b/gi, "ИИ")
  .replace(/\b(?:PM_[A-Z_]+|METRIKA_(?:COUNTER_ID|TOKEN)|YANDEX_SEARCH_(?:API_KEY|FOLDER_ID))\b/g, "параметры подключения");
const resultMode = (status) => status === "ok" ? "Результат ИИ" : "Аналитический режим";
const resultNotice = (status) => status === "invalid_response"
  ? "Ответ ИИ не прошёл проверку. Показан результат аналитического расчёта"
  : status === "unavailable" ? "Доступен аналитический режим" : "";
const dataModeLabel = (mode) => ({
  local: "рабочие данные", synthetic: "синтетические данные", demo_counter: "тестовый счётчик",
})[mode] || "режим данных не указан";
const visibleStatus = (status) => ({
  ok: "готово", complete: "полные данные", partial: "данные неполные", missing: "нет данных",
  disabled: "выключено", unavailable: "недоступно", error: "ошибка проверки", ready: "готово",
  not_checked: "ещё не проверено", skipped: "пропущено", snippet: "поисковый фрагмент",
  primary: "первоисточник", sampled: "выборка", empty: "ничего не найдено", running: "выполняется",
  failed: "проверка не прошла", unknown: "неизвестно",
})[status] || "статус не уточнён";
const workspaceMode = () => /^Синтетическая/i.test(data.source) ? "Демонстрационный режим" : "Рабочий режим";
const numberRu = new Intl.NumberFormat("ru-RU", {maximumFractionDigits: 2});
function presentTime(value) {
  if (!value) return "";
  if (/^\d{4}-\d{2}-\d{2}$/.test(value)) return dateRu(value);
  const time = new Date(value);
  return Number.isNaN(time.getTime()) ? value
    : time.toLocaleString("ru-RU", {dateStyle: "short", timeStyle: "short", timeZone: "UTC"}) + " UTC";
}
function presentValue(value) {
  if (value == null) return "нет данных";
  if (typeof value === "number") return numberRu.format(value);
  if (typeof value === "boolean") return value ? "да" : "нет";
  if (Array.isArray(value)) return value.map(presentValue).join(", ");
  if (typeof value === "object") return Object.entries(value)
    .map(([name, item]) => name + " — " + presentValue(item)).join(" · ");
  return /^\d{4}-\d{2}-\d{2}/.test(value) ? presentTime(value) : publicCopy(value);
}
const node = (tag, t) => {
  const e = document.createElement(tag);
  e.textContent = publicCopy(t);
  return e;
};
const text = (id, t) => {
  $(id).textContent = publicCopy(t);
};
const values = (f) => Object.fromEntries(new FormData(f));
const dateRu = (s) => s.split("-").reverse().join(".");
const workspaces = new Map([...document.querySelectorAll("[data-workspace]")].map((item) => [item.id, item]));
const mobileLayout = matchMedia("(max-width: 760px)");
let visibleWorkspace, reportLink, reportRequest = 0, renderedReportId, returnFromChat = "overview";
let connectionSettings, lastModelResult;
let recommendationReport;
const reportCache = new Map();

function setMobileMenu(open, restoreFocus = false) {
  $("#dashboard").classList.toggle("mobile-open", open);
  $("#sidebar").inert = mobileLayout.matches && !open;
  $("#navScrim").hidden = !open;
  $("#mobileMenu").setAttribute("aria-expanded", String(open));
  if (open) {
    const active = $("#sidebar nav a[aria-current]");
    (active?.getClientRects().length ? active : $("#sidebarToggle")).focus();
  }
  else if (restoreFocus) $("#mobileMenu").focus();
}

function openTarget(target) {
  if (!target) return;
  for (let el = target; el; el = el.parentElement) if (el.tagName === "DETAILS") el.open = true;
  if (!target.matches("a, button, input, select, textarea, [tabindex]")) target.tabIndex = -1;
  target.focus({preventScroll: true});
  target.scrollIntoView({block: "nearest"});
}

function showWorkspace(id, focus) {
  const changed = visibleWorkspace !== id;
  visibleWorkspace = id;
  for (const [name, section] of workspaces) section.hidden = name !== id;
  for (const link of document.querySelectorAll("[data-nav]")) {
    if (link.dataset.nav === id) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  const title = workspaces.get(id).querySelector("h1");
  document.title = title.textContent + " · PARADИИGMA MGR";
  if (changed) {
    setMobileMenu(false);
    window.scrollTo(0, 0);
    document.dispatchEvent(new CustomEvent("dashboard-route", {detail: id}));
  }
  if (focus) title.focus({preventScroll: true});
}

function navigate(hash) {
  const fragment = hash.startsWith("#") ? hash : "#" + hash;
  if (location.hash !== fragment) history.pushState(null, "", fragment);
  applyRoute(true);
}

function applyRoute(focus = false) {
  let fragment;
  try { fragment = decodeURIComponent(location.hash.slice(1)); } catch { fragment = ""; }
  const report = /^report=([0-9a-f]{32})(?:&ref=(evidence-.+))?$/.exec(fragment);
  const target = document.getElementById(report ? report[2] || "" : fragment);
  const id = report || fragment.startsWith("evidence-") ? "agent"
    : fragment.startsWith("qwen-turn-") ? "qwen"
    : target?.closest("[data-workspace]")?.id || "overview";
  showWorkspace(id, focus);
  if (id !== "qwen") returnFromChat = location.hash || id;
  if (report) {
    if (reportLink !== report[1]) openLinkedReport(report[1], report[2]);
    else if (renderedReportId === report[1] && target) openTarget(target);
  } else {
    if (target && target !== workspaces.get(id)) openTarget(target);
    reportLink = null;
    reportRequest++;
  }
}

function setActivity(kind, label) {
  const activity = $("#activeInvestigation");
  activity.dataset[kind] = label || "";
  const states = [activity.dataset.agent, activity.dataset.chat].filter(Boolean);
  activity.hidden = !states.length;
  activity.textContent = states.length > 1 ? "Расследование и чат в работе" : states[0] || "";
  activity.href = activity.dataset.agent ? "#agent" : "#qwen";
}

function renderConnections() {
  const badge = $("#connectionStatus");
  const settings = connectionSettings;
  const online = settings && !settings.error;
  const model = !settings ? "Проверяем настройки" : !online ? "Статус подключения неизвестен"
    : !settings.model_configured ? "ИИ-модуль не подключён"
    : lastModelResult || "Подключение настроено, ответ ещё не проверен";
  badge.dataset.state = !settings || !online ? "unknown"
    : lastModelResult === "Ответ ИИ подтверждён" ? "online" : "configured";
  badge.querySelector("strong").textContent = workspaceMode();
  badge.title = model;
  const rows = [["Сервер приложения", !settings ? "Проверяем" : online ? "Отвечает" : settings.error],
    ["ИИ-модуль", model],
    ["Яндекс.Метрика", online ? settings.metrika_configured ? "Настроена" : "Не настроена" : "Статус неизвестен"],
    ["Анализ конкурентов", online ? settings.public_domains.length ? "Источники настроены" : "Источники не настроены" : "Статус неизвестен"],
    ["Мониторинг", online ? settings.monitor_interval ? "Каждые " + settings.monitor_interval + " с" : "Расписание выключено" : "Статус неизвестен"]];
  $("#connectionDetails").replaceChildren(...rows.flatMap(([label, value]) => [node("dt", label), node("dd", value)]));
}

function modelResult(status) {
  if (status === "ok") lastModelResult = "Ответ ИИ подтверждён";
  if (["unavailable", "invalid_response"].includes(status)) lastModelResult = "Последний результат получен аналитическим расчётом";
  renderConnections();
}

const chatPanel = $("#qwen-panel");
if (chatPanel && !offline) {
  $("#qwenUnavailable").hidden = true;
  $("#qwenWorkspace").append(chatPanel);
}
let menuPrefs = {};
try { menuPrefs = JSON.parse(localStorage.getItem("paradiigma.sidebar")) || {}; } catch { /* optional preference */ }
const saveMenu = () => {
  try { localStorage.setItem("paradiigma.sidebar", JSON.stringify(menuPrefs)); } catch { /* optional preference */ }
};
function renderMenu() {
  const collapsed = !!menuPrefs.collapsed;
  $("#dashboard").classList.toggle("sidebar-compact", collapsed);
  $("#sidebarToggle").setAttribute("aria-expanded", String(!collapsed));
  $("#sidebarToggle").setAttribute("aria-label", collapsed ? "Развернуть меню" : "Свернуть меню");
  $("#sidebarToggle span").textContent = collapsed ? "Развернуть меню" : "Свернуть меню";
  for (const button of document.querySelectorAll(".nav-group-toggle")) {
    const group = button.getAttribute("aria-controls");
    const open = menuPrefs[group] !== false;
    button.setAttribute("aria-expanded", String(open));
    document.getElementById(group).classList.toggle("group-closed", !open);
  }
}
$("#sidebarToggle").onclick = () => { menuPrefs.collapsed = !menuPrefs.collapsed; saveMenu(); renderMenu(); };
for (const button of document.querySelectorAll(".nav-group-toggle")) button.onclick = () => {
  const group = button.getAttribute("aria-controls");
  menuPrefs[group] = menuPrefs[group] === false;
  saveMenu(); renderMenu();
};
$("#mobileMenu").onclick = () => setMobileMenu($("#mobileMenu").getAttribute("aria-expanded") === "false", true);
$("#navScrim").onclick = () => setMobileMenu(false, true);
mobileLayout.addEventListener("change", () => setMobileMenu(false));
document.addEventListener("keydown", (event) => {
  if (!$("#dashboard").classList.contains("mobile-open")) return;
  if (event.key === "Escape") {
    setMobileMenu(false, true);
    return;
  }
  if (event.key !== "Tab") return;
  const controls = [$("#mobileMenu"), ...$("#sidebar").querySelectorAll("button, a")]
    .filter((control) => control.getClientRects().length);
  const index = controls.indexOf(document.activeElement);
  if (index < 0 || (event.shiftKey ? index === 0 : index === controls.length - 1)) {
    event.preventDefault();
    controls[event.shiftKey ? controls.length - 1 : 0].focus();
  }
});
document.addEventListener("click", (event) => {
  const link = event.target.closest('a[href^="#"]');
  if (!link || event.button || event.metaKey || event.ctrlKey || event.altKey || event.shiftKey || event.defaultPrevented) return;
  const hash = link.getAttribute("href");
  if (hash === "#workspace-content") return;
  event.preventDefault();
  if (mobileLayout.matches && link.dataset.nav) setMobileMenu(false);
  navigate(hash);
});
window.addEventListener("popstate", () => applyRoute());
window.addEventListener("hashchange", () => applyRoute());
window.dashboard = {
  navigate,
  inChat: () => visibleWorkspace === "qwen",
  closeChat: () => navigate(returnFromChat),
  chatActivity: (label) => setActivity("chat", label),
  modelResult, publicCopy, resultMode, resultNotice, presentTime, presentValue, visibleStatus, dataModeLabel,
};
renderMenu();
setMobileMenu(false);
renderConnections();
function flash(t) {
  text("#toast", t);
  $("#toast").hidden = false;
  clearTimeout(flash.timer);
  flash.timer = setTimeout(() => ($("#toast").hidden = true), 6500);
}
function task() {
  try {
    const saved = localStorage.getItem(key);
    if (saved || !data.release_id) return JSON.parse(saved);

    const legacy = data.release_id.toLowerCase().replaceAll("-", "");
    return JSON.parse(localStorage.getItem("paradiigma." + legacy + ".task.v1"));
  } catch {
    return null;
  }
}
function writeTask(record) {
  localStorage.setItem(key, JSON.stringify(record));
}
async function api(route, input) {
  if (offline)
    throw Error(
      "Для этой функции запустите python app.py serve и откройте адрес из терминала.",
    );
  const r = await fetch("/api/" + route, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
  const a = await r.json();
  if (!r.ok) throw Error(publicCopy(a.error || "Не удалось выполнить запрос"));
  return a;
}
function drawChart(container) {
  const ns = "http://www.w3.org/2000/svg",
    svg = document.createElementNS(ns, "svg");
  const width = mobileLayout.matches ? 500 : 940;
  svg.setAttribute("viewBox", "0 0 " + width + " 285");
  svg.setAttribute("role", "img");
  svg.setAttribute(
    "aria-label",
    "Недельный и четырёхнедельный индексы заказов",
  );
  function add(tag, attrs, label) {
    const e = document.createElementNS(ns, tag);
    for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
    if (label) e.textContent = label;
    svg.append(e);
  }
  const all = data.series.flatMap((s) => s.points);
  if (new Set(all.map((point) => point[0])).size < 2) {
    container.replaceChildren(node("p", "Недостаточно недель для графика."));
    return;
  }
  const max = Math.max(
    50,
    Math.ceil(
      Math.max(...all.flatMap((p) => p.slice(1)).filter(Number.isFinite)) / 50,
    ) * 50,
  );
  const stamps = all.map((p) => Date.parse(p[0])),
    first = Math.min(...stamps),
    last = Math.max(...stamps);
  const x = (d) => 46 + ((width - 78) * (Date.parse(d) - first)) / (last - first),
    y = (v) => 224 - (176 * v) / max;
  add("rect", {
    x: x(data.config.partial_week) - 4,
    y: 40,
    width: 12,
    height: 186,
    fill: "#f8e6ba",
  });
  for (let v = 0; v <= max; v += max / 5) {
    add("line", { x1: 46, x2: width - 22, y1: y(v), y2: y(v), stroke: "#dae4de" });
    add(
      "text",
      { x: 3, y: y(v) + 4, fill: "#456", "font-size": 14 },
      String(v),
    );
  }
  const launch = x(data.launch);
  add("line", {
    x1: launch,
    x2: launch,
    y1: 33,
    y2: 226,
    stroke: "#5c6868",
    "stroke-dasharray": "4 4",
  });
  add(
    "text",
    {
      x: launch - 5,
      y: 23,
      "text-anchor": "end",
      fill: "#294744",
      "font-size": 15,
    },
    "Запуск " + dateRu(data.launch).slice(0, 5),
  );
  data.series.forEach((s, i) => {
    for (const k of [1, 2]) {
      let d = "",
        move = true;
      for (const p of s.points) {
        if (p[k] == null) {
          move = true;
          continue;
        }
        d += (move ? "M" : "L") + x(p[0]) + "," + y(p[k]) + " ";
        move = false;
      }
      add("path", {
        d,
        fill: "none",
        stroke: i ? "#078477" : "#375891",
        "stroke-width": k === 1 ? 1.6 : 3,
        "stroke-dasharray": k === 1 ? "5 4" : "none",
      });
    }
  });
  const dates = data.series[0].points.map((p) => p[0]);
  dates.forEach((d, i) => {
    if (i % 5 === 0 || i === dates.length - 1)
      add(
        "text",
        {
          x: x(d),
          y: 252,
          "text-anchor": "middle",
          fill: "#47606a",
          "font-size": 14,
        },
        dateRu(d).slice(0, 5),
      );
  });
  container.replaceChildren(svg);
}
mobileLayout.addEventListener("change", () => {
  drawChart($("#chart"));
  drawChart($("#overviewChart"));
});
function render() {
  const c = data.config,
    dates =
      dateRu(c.before_start) +
      "–" +
      dateRu(c.before_end) +
      " и " +
      dateRu(c.after_start) +
      "–" +
      dateRu(c.after_end);
  text("#source", (workspaceMode() + (/^Синтетическая/i.test(data.source) ? " · синтетические данные" : "")) + (offline ? " · сохранённый отчёт" : ""));
  text("#release", [data.release_id, "Запуск " + dateRu(data.launch)].filter(Boolean).join(" · "));
  text("#releaseContext", $("#release").textContent);
  text("#overviewPeriod", dates);
  const comparison = data.series[1]?.name;
  text("#legend", "Синий — " + data.series[0].name +
    (comparison ? ", бирюзовый — " + comparison + " (контроль)." : ". Контрольной группы нет.") +
    " Пунктир — недельный индекс, сплошная — среднее за четыре недели. Вертикальный штрих — дата запуска, жёлтая полоса — неполная неделя.");
  text(
    "#period",
    (c.metric === "orders_index"
      ? "Недельный индекс"
      : "Четырёхнедельный индекс") +
      " · " +
      dates +
      " · даты начала недель",
  );
  const cards = $("#cards");
  cards.replaceChildren();
  const table = $("#numbers");
  table.replaceChildren();
  const row = (cells, heading = false) => {
    const tr = node("tr", "");
    for (const v of cells) {
      const cell = node(heading ? "th" : "td", v);
      if (heading) cell.scope = "col";
      tr.append(cell);
    }
    table.append(tr);
  };
  row([
    "Продукт",
    "До",
    "После",
    "Изменение",
    "До " + dateRu(c.partial_week),
    "Изменение",
  ], true);
  for (const s of data.series) {
    const a = node("a", s.name);
    a.href = "#dynamics";
    a.className = "kpi";
    a.append(node("strong", pct(s.delta)), node("small", "Изменение индекса заказов"));
    if (s.delta < -c.alert_percent || s.delta < 0 && s.delta === -c.alert_percent) a.classList.add("kpi-alert");
    cards.append(a);
    row([
      s.name,
      fmt(s.before),
      fmt(s.after),
      pct(s.delta),
      fmt(s.extra),
      pct(s.extra_delta),
    ]);
  }
  const delta = data.series[0].delta;
  const alert = delta != null && delta < 0 && -delta >= c.alert_percent;
  const signalCard = node("a", "Сигналы");
  signalCard.className = alert ? "kpi kpi-signal" : "kpi";
  signalCard.href = "#signal";
  signalCard.append(node("strong", delta == null ? "—" : alert ? "1" : "0"), node("small", "Порог изменения заказов"));
  cards.append(signalCard);
  $("#signal").closest(".panel").classList.toggle("has-signal", alert);
  if (!$("#overviewActions").children.length) $("#overviewActions").append(node("p", "Рекомендации появятся после открытия расследования."));
  text(
    "#signal",
    delta == null ? "Недостаточно данных для проверки порога." :
    delta < 0 && -delta >= c.alert_percent
      ? "Сигнал · снижение превысило или достигло порога " +
          c.alert_percent +
          "%. Причина требует проверки."
      : "Порог падения " + c.alert_percent + "% не достигнут.",
  );
  if ($("#answer").hidden)
    text(
      "#toggle",
      delta != null && delta < -0.05
        ? "Разобрать падение"
        : "Разобрать динамику",
    );
  text(
    "#reachPeriod",
    dateRu(c.reach_start) +
      "–" +
      dateRu(c.reach_end) +
      " · источник cfg_reach_rate, суммы из detail_json.",
  );
  $("#reach").replaceChildren(
    ...Object.entries(data.reach).map(([k, v]) =>
      node(
        "li",
        k +
          " · из " +
          v.den +
          " визитов на страницы семейства в " +
          v.num +
          " выбрали опцию. Покрытие " +
          v.days +
          "/" +
          v.total +
          " дней.",
      ),
    ),
  );
  text(
    "#t08",
    data.backlog.id +
      " · " +
      data.backlog.title +
      " (" +
      data.backlog.status +
      "). Связанная задача в карточке релиза — " + data.release_task + "." +
      (data.release_task && data.backlog.id !== data.release_task ? " Обнаружено расхождение идентификаторов." : ""),
  );
  const f = $("#settings");
  for (const e of f.elements) if (e.name) e.value = c[e.name];
  drawChart($("#chart"));
  drawChart($("#overviewChart"));
  text("#overviewLegend", $("#legend").textContent);
  sendSignal();
}
function showHits(hits) {
  const c = $("#hits");
  c.replaceChildren();
  if (!hits.length)
    c.append(node("p", "Прямых подтверждений по запросу не найдено."));
  for (const x of hits) {
    const a = node("article", "");
    a.id = "src-" + x.id;
    a.append(node("strong", x.ref), node("small", [x.kind, presentTime(x.observed_at || x.retrieved)].filter(Boolean).join(" · ")), node("p", x.text));
    c.append(a);
  }
}
function renderSaved() {
  const r = task();
  text(
    "#saved",
    r
      ? [
          r.id + " · " + r.title,
          r.hypothesis,
          "Проверка: " + r.steps,
          "Цель 4 недель: " + fmt(r.target) + " · " + r.period,
          "Связи: " + [data.release_id, data.backlog.id, r.tag].filter(Boolean).join(" · "),
          "Источники: " + r.sources.join(" · "),
          r.decision ? "Решение: " + r.decision : "Решение ожидает проверки",
        ].join("\n")
      : "Сохранённых задач пока нет.",
  );
  document
    .querySelectorAll(".draft")
    .forEach(
      (b) =>
        (b.textContent = r
          ? "Открыть сохранённую задачу"
          : "Создать задачу проверки"),
    );
}
function period(c) {
  return (
    dateRu(c.before_start) +
    "–" +
    dateRu(c.before_end) +
    " / " +
    dateRu(c.after_start) +
    "–" +
    dateRu(c.after_end)
  );
}
function edit() {
  const r = task(),
    f = $("#task");
  f.hidden = false;
  f.elements.title.value =
    r?.title || "Проверить выбор лицензии и передачу события корзины";
  f.elements.hypothesis.value =
    r?.hypothesis ||
    proposal ||
    "Возможны сбой пути покупки и неполная регистрация событий. Причина падения неизвестна.";
  f.elements.steps.value =
    r?.steps ||
    "Записать устройство, вариант, время и наличие лицензии в корзине. Сверить действие с событием и связанной задачей.";
  f.elements.target.value = r?.target ?? data.series[0].rolling_base?.toFixed(2) ?? "";
  text(
    "#criterion",
    "Исходная база сравнения и окно после релиза: " +
      (r?.period || period(data.config)) +
      ". По умолчанию цель равна среднему четырёхнедельному индексу по базовым неделям. Подтвердите значение до исправления.",
  );
  navigate("backlog");
  openTarget(f);
  f.elements.title.focus();
}
document.querySelectorAll(".draft").forEach((b) => (b.onclick = edit));
$("#task").onsubmit = (e) => {
  e.preventDefault();
  const f = e.currentTarget,
    fields = values(f),
    old = task();
  const a = {
    ...fields,
    target: Number(fields.target),
    id: ["PM", data.release_id, data.backlog.id].filter(Boolean).join("-"),
    tag: "сгенерирована ИИ-помощником",
    period: old?.period || period(data.config),
    sources: old?.sources || ["Продажи", "cfg_reach_rate", data.backlog.id, ...proof],
    config: old?.config || { ...data.config },
    baseline:
      old?.baseline ||
      data.series.map((s) => ({ mean: s.before, rolling: s.rolling_base })),
    updated: new Date().toISOString(),
  };
  if (old?.decision) {
    a.decision = old.decision;
    a.history = old.history || [];
    if (old.target !== a.target) {
      a.history = [
        ...a.history,
        { decision: old.decision, target: old.target },
      ];
      delete a.decision;
    }
  }
  try {
    writeTask(a);
    f.hidden = true;
    renderSaved();
    assessment = null;
    $("#effectResult").textContent =
      "Цель сохранена. Ожидаем данные после исправления";
    flash("Задача сохранена в этом браузере");
  } catch {
    flash("Браузер отклонил запись. Черновик остаётся открыт.");
  }
};
$("#cancel").onclick = () => {
  $("#task").hidden = true;
};
$("#toggle").onclick = () => {
  const a = $("#answer");
  a.hidden = !a.hidden;
  $("#toggle").setAttribute("aria-expanded", String(!a.hidden));
  if (a.hidden) render();
  else text("#toggle", "Свернуть разбор");
};
$("#settings").onsubmit = async (e) => {
  e.preventDefault();
  try {
    const c = values(e.currentTarget);
    c.alert_percent = Number(c.alert_percent);
    data = await api("overview", c);
    proposal = "";
    proof = [];
    text("#ai", "");
    render();
    flash("Показатели пересчитаны");
  } catch (err) {
    flash(err.message);
  }
};
async function findSources(e) {
  e.preventDefault();
  const query = e.currentTarget.elements.query.value;
  try {
    const hits = offline
      ? boot.evidence.filter((x) =>
          query
            .toLowerCase()
            .split(/\s+/)
            .some(
              (t) =>
                t.length > 2 &&
                (x.text + " " + x.ref).toLowerCase().includes(t.slice(0, 5)),
            ),
        )
      : (await api("search", { query })).hits;
    showHits(hits);
    text("#researchStatus", "Найдено источников: " + hits.length);
    if (e.target.id === "search" && visibleWorkspace === "agent") navigate("sources");
  } catch (err) {
    flash(err.message);
  }
}
$("#search").onsubmit = findSources;
$("#researchSearch").onsubmit = findSources;
$("#investigateResearch").onclick = () => {
  const query = $("#researchSearch").elements.query.value.trim();
  if (query && !$("#infer").disabled) $("#search").elements.query.value = query;
  navigate("agent");
};
$("#compareOffers").onclick = () => {
  if (!$("#infer").disabled) $("#search").elements.query.value = "Сравни предложения и источники конкурентов";
  navigate("agent");
};
let pendingMemoryScope = null;

async function runWithProgress(input) {
  const job = await api("analysis/start", input);
  const deadline = Date.now() + (job.wait_seconds ?? 270) * 1000;
  const names = {queue: "В очереди к ИИ", synthesis: "Синтез гипотез",
    sales: "Продажи", funnel: "Воронка", releases: "Релизы", backlog: "Бэклог",
    research: "Исследования", competitors: "Конкуренты", metrika: "Метрика",
    public_competitors: "Публичные источники", effect: "Эффект", compare_history: "Сравнение периодов",
    search_history: "Поиск в памяти", competitor_history: "История конкурентов"};
  const statuses = {waiting: "ожидание", running: "выполняется", ok: "готово", missing: "нет данных", error: "ошибка",
    unavailable: "недоступно", partial: "частично", disabled: "выключено"};
  while (Date.now() < deadline) {
    const result = await api("analysis/status", {job_id: job.job_id});
    const failed = result.status === "failed";
    const events = result.phases.filter((event) => ["queue", "tool", "synthesis"].includes(event.step));
    // The API sends a history of updates. On failure, show only the final status of each phase.
    const phases = failed
      ? [...new Map(events.map((event) => [JSON.stringify([event.step, event.tool]), event])).values()]
      : events;
    $("#runPhases").replaceChildren(...phases.map((event) => {
      const status = failed && ["waiting", "running"].includes(event.status)
        ? "прервано"
        : statuses[event.status] || visibleStatus(event.status);
      return node("li", (names[event.tool] || names[event.step] || event.tool) + " · " + status);
    }));
    if (failed) throw Error(result.message || "Расследование завершилось с ошибкой. Проверьте настройки и источники.");
    if (events.length) setActivity("agent", names[events.at(-1).tool] || names[events.at(-1).step] || "Идёт расследование");
    if (result.status === "ready") return api("analysis/report", {run_id: result.run_id});
    await new Promise((resolve) => setTimeout(resolve, 650));
  }
  throw Error("Время ожидания истекло. Проверьте журнал для завершённого отчёта.");
}

$("#infer").onclick = async (e) => {
  e.target.disabled = true;
  setActivity("agent", "Идёт расследование");
  const ai = $("#ai");
  ai.textContent = "Идёт подбор источников и проверок…";
  try {
    history.replaceState(null, "", "#agent");
    reportLink = null;
    reportRequest++;
    renderInvestigation(null);
    const request = {
      query: $("#search").elements.query.value,
      config: data.config,
    };
    if ($("#includeMeter").checked) {
      request.metrika = $("#compareMeter").checked ? { compare: true } : values($("#meter"));
    }
    if ($("#publicProduct").value) {
      const selected = (id) => Array.from($(id).selectedOptions, (o) => o.value);
      request.web = { product: $("#publicProduct").value,
        domains: selected("#publicDomains"), topics: selected("#publicTopics") };
    }
    if (pendingMemoryScope) {
      request.memory = pendingMemoryScope;
      pendingMemoryScope = null;
    }
    $("#runPhases").replaceChildren();
    const res = await runWithProgress(request);
    reportCache.set(res.run_id, res);
    if (!reportLink && (!renderedReportId || visibleWorkspace === "agent")) renderInvestigation(res);
    modelResult(res.llm_status);
    if (visibleWorkspace === "agent" && !reportLink) {
      history.replaceState(null, "", "#report=" + res.run_id);
      reportLink = res.run_id;
    }
    reloadRuns();
    showHits(res.hits);
    proposal = res.hypothesis || res.proposal || "";
    proof = res.refs || [];
    ai.textContent = [
      resultMode(res.llm_status),
      resultNotice(res.llm_status),
      presentValue(res.seconds) + " с",
    ]
      .filter(Boolean)
      .join(" · ");
    for (const id of proof) {
      const link = node("a", " [" + id + "]");
      link.href = "#report=" + res.run_id + "&ref=evidence-" + encodeURIComponent(id);
      ai.append(link);
    }
  } catch (err) {
    ai.textContent = publicCopy(err.message);
    text("#runSummary", "Расследование не завершено.");
  } finally {
    e.target.disabled = false;
    setActivity("agent", "");
  }
};
function renderInvestigation(report) {
  const isRelease = report?.evidence.some((item) => item.id === "release:current" && item.ref === data.release_id);
  if (isRelease) document.dispatchEvent(new CustomEvent("qwen-release-report", {detail: report.run_id}));
  const container = $("#runDetails");
  container.replaceChildren();
  text("#runSummary", report
    ? [report.summary, "Сохранённый отчёт", resultMode(report.llm_status), dataModeLabel(report.data_mode)].filter(Boolean).join(" · ")
    : "Собираем доказательства…");
  renderedReportId = report?.run_id;
  if (!report) return;
  $("#runPhases").replaceChildren();
  const notice = resultNotice(report.llm_status);
  if (notice) container.append(node("p", notice));
  const recommendations = report.recommended_actions || [];
  if (!recommendationReport) renderOverviewRecommendations(report);
  const competitorList = $("#competitorEvidence"), releaseList = $("#releaseEvidence");
  const comparisons = report.evidence.filter((item) => ["competitor_local", "competitor_web"].includes(item.kind));
  const releases = report.evidence.filter((item) => item.kind === "release");
  competitorList.replaceChildren(...(comparisons.length
    ? comparisons.map((item) => evidenceLinks([item.offer?.product || item.attributes?.product, item.domain, item.ref, presentTime(item.observed_at || item.retrieved)].filter(Boolean).join(" · ") + " · " + visibleStatus(item.quality) + " · " +
      (item.freshness === "recent" ? "свежий источник" : "актуальность требует проверки") +
      (item.offer?.price != null ? " · " + presentValue(item.offer.price) + " " + (item.offer.currency || "") : ""), [item.id]))
    : [node("p", "В отчёте нет подходящих конкурентных материалов. Проверьте сохранённые наблюдения в базе знаний или добавьте разрешённые источники в расследование.")]));
  releaseList.replaceChildren(...(releases.length
    ? releases.map((item) => evidenceLinks(item.ref + " · " + (presentTime(item.observed_at) || "дата неизвестна"), [item.id]))
    : [node("p", "Релизы в отчёте отсутствуют.")]));
  const status = { ok: "доступен", partial: "частично", disabled: "выключен",
    missing: "нет данных", unavailable: "недоступен", error: "ошибка", not_checked: "ещё не проверен" };
  const section = (heading, open = false) => {
    const block = node("details", "");
    block.open = open;
    block.append(node("summary", heading));
    container.append(block);
    return block;
  };
  const sourceBlock = section("Доступность источников");
  const labels = {sales: "Продажи", research: "Исследования", release: "Релиз",
    backlog: "Бэклог", funnel: "Воронка", historical_metrics: "Исторические метрики", memory: "База знаний", competitor_web: "Конкуренты в интернете",
    competitor_local: "Локальные данные о конкурентах", metrika: "Яндекс.Метрика",
    effect: "Замер после выпуска"};
  for (const [name, value] of Object.entries(report.sources_status)) {
    sourceBlock.append(node("p", (labels[name] || "Дополнительный источник") + " · " + (status[value.status] || visibleStatus(value.status)) +
      (value.reason === "different_current_data_mode" ? " · источник относится к другому режиму данных" : "")));
  }
  const steps = (report.trace || []).filter((item) => item.step === "tool");
  if (steps.length) {
    const history = section("Действия агента");
    const tools = { sales: "Продажи и контроль", funnel: "Воронка", metrika: "Метрика",
      releases: "Релизы", research: "Исследования", competitors: "Конкуренты",
      backlog: "Бэклог", public_competitors: "Публичные источники", effect: "Оценка эффекта" };
    for (const step of steps) {
      history.append(evidenceLinks(
        (tools[step.tool] || "Дополнительная проверка") + " · " + (status[step.status] || visibleStatus(step.status)) +
        (step.actor === "qwen" ? " · выбор ИИ" : " · автоматический сбор"),
        step.evidence_ids,
      ));
    }
  }
  const findings = section("Наблюдения");
  for (const item of report.findings) findings.append(evidenceLinks(item.text, item.evidence_ids));
  const hypotheses = section(report.llm_status === "ok" ? "Гипотезы ИИ" : "Гипотезы по правилам", true);
  for (const item of report.hypotheses) {
    const row = evidenceLinks(item.problem + " · " + item.cause, item.evidence_ids);
    row.append(node("p", "Проверка до разработки · " + item.verify));
    row.append(node("p", "Ожидаемый результат · " + item.expected));
    row.append(node("p", "Нужны данные · " + item.missing));
    row.append(node("p", "Риск · " + item.risk));
    if (item.alternative_explanations?.length) {
      row.append(node("p", "Альтернативы · " + item.alternative_explanations.join(" · ")));
    }
    if (item.business_impact) row.append(node("p", "Влияние · " + item.business_impact));
    row.append(node("p", "Уверенность · " + ({low: "низкая", medium: "средняя", high: "высокая"}[item.confidence])));
    if (item.contradictions.length) row.append(evidenceLinks("Противоречия", item.contradictions));
    if (item.citation_ambiguities?.length) row.append(evidenceLinks("Неоднозначные ссылки", item.citation_ambiguities));
    hypotheses.append(row);
  }
  if (!report.hypotheses.length) hypotheses.append(node("p", "Проверяемых гипотез нет."));
  const limits = section("Ограничения");
  for (const item of report.limitations) limits.append(node("p", item));
  if (report.backlog_matches.length) {
    const backlog = section("Похожие задачи");
    for (const item of report.backlog_matches) backlog.append(node("p", item.id + " · " + item.title));
  }
  if (report.evidence_changes) {
    const change = report.evidence_changes;
    const updates = section("Изменения доказательств");
    for (const [label, refs] of [["Новые", change.added], ["Обновлённые", change.updated]]) {
      if (refs.length) updates.append(evidenceLinks(label, refs));
    }
    if (change.removed.length) updates.append(node("p", "Убрано из контекста: " + change.removed.length));
    if (!change.added.length && !change.updated.length && !change.removed.length) {
      updates.append(node("p", "Состав доказательств и показатели не изменились."));
    }
  }
  if (report.request_context && !offline) {
    const again = node("button", "Повторить с текущими данными");
    again.onclick = async () => {
      again.disabled = true;
      try {
        const refreshed = await api("analysis/reopen", {run_id: report.run_id});
        reportCache.set(refreshed.run_id, refreshed);
        modelResult(refreshed.llm_status);
        reloadRuns();
        if (visibleWorkspace === "agent" && renderedReportId === report.run_id) navigate("report=" + refreshed.run_id);
        else flash("Новое расследование сохранено в журнале.");
      }
      catch (error) { flash(error.message); }
      finally { again.disabled = false; }
    };
    container.append(again);
  }
  if (report.proposed_tasks.length) {
    const tasks = section("Предложения в бэклог", true);
    for (const item of report.proposed_tasks) tasks.append(proposalForm(report, item));
  }
  if (recommendations.length) {
    const actions = section("Рекомендации", true);
    actions.append(...recommendations.map((item) => node("p", item)));
  }
  const evidence = section("Доказательства");
  for (const item of report.evidence) {
    const article = node("article", "");
    article.id = "evidence-" + item.id;
    article.append(node("strong", item.ref + " · " + dataModeLabel(item.data_mode) + " · " + visibleStatus(item.quality)));
    if (item.text) article.append(node("p", item.text));
    if (item.values) {
      const details = node("details", "");
      details.append(node("summary", "Данные источника"),
        node("p", Object.entries(item.values).map(([k, v]) => k + " — " + presentValue(v)).join(" · ")));
      article.append(details);
    }
    if (item.locator && item.kind === "competitor_web") {
      try {
        const url = new URL(item.locator);
        if (["https:", "http:"].includes(url.protocol)) {
          const link = node("a", "Открыть источник");
          link.href = url.href;
          link.rel = "noopener noreferrer";
          link.target = "_blank";
          article.append(link);
        }
      } catch { /* omit malformed links */ }
    }
    evidence.append(article);
  }
}

function evidenceLinks(label, ids) {
  const item = node("div", "");
  item.append(node("p", label));
  for (const id of ids) {
    const link = node("a", " [" + id + "] ");
    link.href = "#report=" + renderedReportId + "&ref=evidence-" + encodeURIComponent(id);
    item.append(link);
  }
  return item;
}

function proposalForm(report, item) {
  const form = node("form", "");
  const baseline = report.effect_plan.baseline;
  form.append(node("strong", item.title), node("p", item.tag),
    node("p", "Исходный четырёхнедельный индекс · " + fmt(baseline)),
    node("p", item.priority.impact + " · " + item.verification));
  if (item.existing_tasks?.length) {
    for (const existing of item.existing_tasks) {
      form.append(node("p", "Проверить приоритет существующей задачи · " + existing.id + " · " + existing.title));
    }
  }
  if (report.effect_plan.target_basis) form.append(node("p", report.effect_plan.target_basis));
  const label = node("label", "Целевой индекс");
  const target = document.createElement("input");
  target.type = "number";
  target.min = ".01";
  target.step = ".01";
  label.append(target);
  const controlLabel = node("label", "Минимум для контрольной группы");
  const control = document.createElement("input");
  control.type = "number";
  control.min = "0";
  control.step = ".01";
  controlLabel.append(control);
  const stopLabel = node("label", "Порог отказа от дальнейшей разработки");
  const stop = document.createElement("input");
  stop.type = "number";
  stop.min = "0";
  stop.step = ".01";
  stopLabel.append(stop);
  const reasonLabel = node("label", "Причина решения");
  const reason = document.createElement("input");
  reason.maxLength = 800;
  reasonLabel.append(reason);
  const accept = node("button", "Согласовать");
  accept.disabled = baseline == null;
  accept.title = baseline == null ? "Для согласования нужен исходный индекс" : "";
  const reject = node("button", "Отклонить");
  reject.type = "button";
  const output = node("p", "");
  output.setAttribute("role", "status");
  form.append(label, controlLabel, stopLabel, reasonLabel, accept, reject, output);
  const submit = async (action) => {
    if (action === "accept" && (!target.value || !target.checkValidity() || !control.checkValidity() || !stop.checkValidity())) {
      output.textContent = "Укажите корректную численную цель.";
      return;
    }
    accept.disabled = reject.disabled = true;
    try {
      await api("tasks/decide", {
        run_id: report.run_id, proposal_id: item.id, action,
        target: action === "accept" ? Number(target.value) : null,
        control_min: action === "accept" && control.value ? Number(control.value) : null,
        stop_if_below: action === "accept" && stop.value ? Number(stop.value) : null,
        reason: reason.value.trim() || null,
        idempotency_key: report.run_id + "/" + item.id + "/" + action,
      });
      output.textContent = action === "accept" ? "Задача согласована в локальном бэклоге." : "Предложение отклонено.";
      await reloadRuns();
    } catch (error) {
      output.textContent = error.message;
      accept.disabled = baseline == null;
      reject.disabled = false;
    }
  };
  form.onsubmit = (event) => { event.preventDefault(); submit("accept"); };
  reject.onclick = () => submit("reject");
  return form;
}

function effectReviewText(review) {
  const details = [review.mode === "simulation" ? "Симуляция" : "",
    review.recommendation];
  if (review.measurement) {
    details.push("Исходный индекс " + fmt(review.plan.baseline), "Цель " + fmt(review.plan.target),
      "Контрольный ряд " + fmt(review.measurement.astra), "Измеренный индекс " + fmt(review.measurement.kaspersky));
    if (review.control_adjusted_change_pp != null) {
      details.push("Разница изменений с контролем " + fmt(review.control_adjusted_change_pp) + " п. п.");
    }
  }
  for (const guard of review.side_effects || []) {
    if (guard.status !== "ok") details.push("Контроль " + guard.metric + " — " +
      (guard.status === "missing" ? "нет замера" : "ниже минимума"));
  }
  if (review.external_factors?.length) details.push("Внешние факторы · " + review.external_factors.join(", "));
  return details.filter(Boolean).join(" · ");
}

function renderOverviewRecommendations(report) {
  recommendationReport = report.run_id;
  const recommendations = (report.recommended_actions || []).slice(0, 3);
  const list = node("ol", "");
  if (recommendations.length) list.append(...recommendations.map((item) => node("li", item)));
  const link = node("a", "Открыть отчёт");
  link.href = "#report=" + report.run_id;
  $("#overviewActions").replaceChildren(recommendations.length ? list : node("p", "В последнем отчёте нет приоритетных рекомендаций."), link);
}

async function reloadRuns() {
  try {
    const [journal, backlog] = await Promise.all([api("analysis/journal", {}), api("tasks", {})]);
    const history = $("#runHistory"), tasks = $("#serverTasks");
    const overview = $("#overviewRuns");
    history.replaceChildren();
    overview.replaceChildren();
    if (!journal.runs.length) overview.append(node("p", "Пока нет сохранённых расследований."));
    const emailStatuses = {
      disabled: "отключена", configuration_error: "проверьте настройки",
      blocked_test_mode: "доступна только для синтетических данных", pending: "в очереди",
      retry: "ожидает повтора", sending: "отправляется", submitted: "принято SMTP",
      failed: "ошибка отправки", uncertain: "результат неизвестен", expired: "срок отправки истёк",
    };
    for (const item of journal.notifications || []) {
      if (item.status !== "ready") continue;
      const mail = emailStatuses[item.email_status];
      const alert = node("p", "Сигнал мониторинга · " + presentTime(item.created) +
        (mail ? " · почта " + mail : ""));
      const link = node("a", "Отчёт по сигналу");
      link.href = "#report=" + item.run_id;
      alert.append(link);
      history.append(alert);
    }
    for (const [index, item] of journal.runs.entries()) {
      const label = item.summary || (item.data_mode === "synthetic" ? "Синтетическое расследование заказов" : "Сохранённое расследование");
      const link = node("a", label);
      link.className = "report-row";
      link.href = "#report=" + item.run_id;
      link.append(node("small", presentTime(item.created)));
      history.append(link);
      if (index < 3) overview.append(link.cloneNode(true));
    }
    if (!history.children.length) history.append(node("p", "Пока нет сохранённых расследований."));
    const latest = journal.runs[0]?.run_id;
    if (latest && latest !== recommendationReport) {
      const current = recommendationReport;
      const load = reportCache.has(latest) ? Promise.resolve(reportCache.get(latest)) : api("analysis/report", {run_id: latest});
      load.then((report) => {
        reportCache.set(latest, report);
        if (recommendationReport === current) renderOverviewRecommendations(report);
      }).catch(() => {
        if (!recommendationReport) text("#overviewActions", "Не удалось загрузить рекомендации из последнего отчёта.");
      });
    }
    tasks.replaceChildren();
    if (!backlog.tasks.length) tasks.append(node("p", "Пока нет согласованных или отклонённых предложений."));
    for (const item of backlog.tasks) {
      const row = node("article", "");
      row.append(node("h3", item.title), node("p", [item.id, presentTime(item.created), item.decision === "accept" ? "согласована" : "отклонена",
        item.tag].filter(Boolean).join(" · ")));
      if (item.run_id) {
        const source = node("a", "Открыть расследование");
        source.href = "#report=" + item.run_id;
        row.append(source);
      }
      if (item.decision === "accept") {
        const label = node("label", item.data_mode === "synthetic" ? "JSON для симуляции" : "JSON с наблюдениями");
        const file = document.createElement("input");
        file.type = "file";
        file.accept = ".json";
        label.append(file);
        const status = node("p", "");
        status.setAttribute("role", "status");
        file.onchange = async () => {
          try {
            const picked = file.files[0];
            if (!picked) return;
            if (picked.size > 100000) throw Error("Файл слишком большой");
            const review = await api("tasks/review", {
              task_id: item.id, observation: JSON.parse(await picked.text()),
              mode: item.data_mode === "synthetic" ? "simulation" : "actual",
            });
            status.textContent = effectReviewText(review);
          } catch (error) { status.textContent = error.message; }
        };
        if (item.latest_review) status.textContent = effectReviewText(item.latest_review);
        const exportButton = node("button", "Экспорт Redmine");
        exportButton.type = "button";
        exportButton.className = "secondary";
        exportButton.onclick = async () => {
          exportButton.disabled = true;
          try {
            const payload = await api("tasks/redmine-export", {task_id: item.id});
            const url = URL.createObjectURL(new Blob([JSON.stringify(payload, null, 2)], {type: "application/json"}));
            const link = node("a", "");
            link.href = url;
            link.download = "redmine-task.json";
            link.click();
            setTimeout(() => URL.revokeObjectURL(url), 1000);
          } catch (error) { flash(error.message); }
          finally { exportButton.disabled = false; }
        };
        row.append(status, exportButton, label, node("small", "Для оценки приложите JSON с четырьмя полными неделями."));
      }
      if (window.qwenAskWithScope && item.decision === "accept") {
        const ask = node("button", "Обсудить эффект в чате");
        ask.onclick = () => window.qwenAskWithScope("Какой эффект показала согласованная задача?", {task_id: item.id});
        row.append(ask);
      }
      tasks.append(row);
    }
  } catch (error) {
    text("#runHistory", error.message);
    text("#overviewRuns", "Журнал недоступен. Попробуйте обновить его.");
    text("#serverTasks", error.message);
  }
}
$("#reloadRuns").onclick = reloadRuns;
$("#dailyDigest").onclick = async () => {
  try {
    const digest = await api("analysis/daily", {});
    const lines = [["Рабочие данные", digest.groups.local], ["Синтетические", digest.groups.synthetic]];
    text("#digestStatus", digest.date_utc + " UTC · " + lines.map(([label, group]) =>
      label + " · расследований " + group.runs + ", на проверке " + group.pending_review).join(" · "));
  } catch (error) { text("#digestStatus", error.message); }
};
$("#checkNow").onclick = async () => {
  $("#checkNow").disabled = true;
  try {
    const check = await api("analysis/check", { config: data.config });
    const labels = { signal: "Порог достигнут", normal: "Порог не достигнут",
      missing: "Недостаточно данных", busy: "Проверка уже идёт" };
    text("#checkStatus", labels[check.status] + (check.notification ? " · новый сигнал" : ""));
  } catch (error) {
    text("#checkStatus", error.message);
  } finally {
    $("#checkNow").disabled = false;
  }
};
if (!offline) api("analysis/settings", {}).then((settings) => {
  connectionSettings = settings;
  renderConnections();
  text("#analysisDiagnostics", [
    settings.model_configured ? "ИИ-модуль настроен" : "ИИ-модуль не подключён",
    settings.metrika_configured ? "Параметры Метрики заданы" : "Метрика не настроена",
    settings.monitor_interval ? "Мониторинг каждые " + settings.monitor_interval + " с" : "Мониторинг по расписанию выключен",
  ].join(" · "));
  if (settings.public_products.length && settings.public_domains.length) {
    $("#publicSearch").hidden = false;
    for (const value of settings.public_products) $("#publicProduct").append(node("option", value));
    for (const value of settings.public_domains) $("#publicDomains").append(node("option", value));
  }
}).catch((error) => { connectionSettings = {error: error.message}; renderConnections(); });
else {
  connectionSettings = {error: "Открыт сохранённый HTML-отчёт"};
  renderConnections();
  text("#analysisDiagnostics", "Запустите локальный сервер, чтобы проверить подключения.");
}
$("#txt").onchange = async (e) => {
  const f = e.target.files[0];
  if (!f) return;
  if (f.size > 120000) {
    flash("Выберите небольшой текстовый файл");
    return;
  }
  $("#upload").elements.text.value = await f.text();
  $("#upload").elements.name.value = f.name;
};
$("#upload").onsubmit = async (e) => {
  e.preventDefault();
  try {
    const r = await api("import", values(e.currentTarget));
    flash("Добавлено абзацев: " + r.added + ". Они доступны в поиске.");
    e.target.reset();
  } catch (err) {
    flash(err.message);
  }
};
async function evaluateEffect() {
  assessment = null;
  const mode = $("#mode").value,
    record = task();
  if (mode === "actual" && !observation) {
    text("#effectResult", "Ожидаем данные после исправления");
    return;
  }
  if (!record) {
    text(
      "#effectResult",
      "Сначала согласуйте цель и сохраните задачу проверки.",
    );
    return;
  }
  try {
    const r = await api("effect", { mode, observation, target: record.target });
    assessment = r;
    text(
      "#effectResult",
      (mode === "demo"
        ? "Симуляция · Синтетический тест · "
        : "Загруженные данные команды · ") +
        r.status +
        (r.start
          ? " · " +
            dateRu(r.start) +
            "–" +
            dateRu(r.end) +
            " · " + data.series[0].name + " " +
            fmt(r.kaspersky) +
            " при цели " +
            fmt(r.target) +
            " · " + data.series[1].name + " " +
            fmt(r.astra) +
            " (база " +
            fmt(record.baseline[1]?.rolling) +
            ")" +
            (r.before_fix
              ? " · предыдущие четыре полные недели: " + data.series[0].name + " " +
                fmt(r.before_fix.kaspersky) +
                ", " + data.series[1].name + " " +
                fmt(r.before_fix.astra)
              : " · четыре полные недели до исправления не переданы")
          : ""),
    );
  } catch (err) {
    text("#effectResult", err.message);
  }
}
$("#mode").onchange = evaluateEffect;
$("#weeksFile").onchange = async (e) => {
  const f = e.target.files[0];
  if (!f) return;
  try {
    if (f.size > 100000) throw Error("Файл слишком большой");
    observation = JSON.parse(await f.text());
    if (!Array.isArray(observation?.rows) || !observation.fix)
      throw Error("В JSON укажите дату исправления (fix) и четыре недели наблюдений (rows)");
    $("#mode").value = "actual";
    await evaluateEffect();
  } catch (err) {
    observation = null;
    assessment = null;
    text("#effectResult", err.message);
  }
};
$("#saveDecision").onclick = () => {
  const r = task();
  if (!r || $("#mode").value !== "actual" || !assessment?.start) {
    flash(
      "Для решения нужны сохранённая задача и четыре полные недели фактических данных.",
    );
    return;
  }
  try {
    r.decision = $("#decision").value;
    r.history = [
      ...(r.history || []),
      {
        decision: r.decision,
        effect: assessment,
        at: new Date().toISOString(),
      },
    ];
    writeTask(r);
    renderSaved();
    flash("Решение сохранено");
  } catch {
    flash("Не удалось сохранить решение");
  }
};
$("#notify").onclick = async () => {
  if (!("Notification" in window))
    return flash("Браузер не поддерживает системные уведомления.");
  try {
    const allowed = await Notification.requestPermission();
    if (allowed !== "granted")
      return flash("Уведомления внутри обзора остаются доступны.");
    localStorage.setItem("pm.alerts", "on");
    sendSignal();
    flash("Уведомления включены для открытого обзора.");
  } catch {
    flash("Системное уведомление недоступно.");
  }
};
function sendSignal() {
  try {
    const d = data.series[0].delta,
      c = data.config,
      k = [c.metric, period(c), c.alert_percent, d].join("|");
    if (
      d != null &&
      d < 0 &&
      -d >= c.alert_percent &&
      localStorage.getItem("pm.alerts") === "on" &&
      Notification.permission === "granted" &&
      localStorage.getItem("pm.signal") !== k
    ) {
      new Notification("ParadiigmaMGR", {
        body: data.series[0].name + " " + pct(d) + " · " + period(c),
      });
      localStorage.setItem("pm.signal", k);
    }
  } catch {}
}
const dayString = (d) => d.toISOString().slice(0, 10);
$("#meter").elements.end.value = dayString(new Date(Date.now() - 86400000));
$("#meter").elements.start.value = dayString(
  new Date(Date.now() - 7 * 86400000),
);
$("#meter").onsubmit = async (e) => {
  e.preventDefault();
  text("#meterResult", "Запрос к API…");
  try {
    const r = await api("metrika", values(e.currentTarget));
    text(
      "#meterResult",
      r.available
        ? r.source +
            " · " +
            presentTime(r.start) +
            "–" +
            presentTime(r.end) +
            " · визитов " +
            presentValue(r.value) +
            " · получено " +
            presentTime(r.retrieved) +
            (r.sampled ? " · выборка" : "")
        : r.reason === "not_configured" || !connectionSettings?.metrika_configured
          ? "Метрика недоступна: подключение счётчика не настроено."
          : publicCopy(r.message || "Метрика недоступна для выбранного периода."),
    );
  } catch (err) {
    text("#meterResult", err.message);
  }
};
window.addEventListener("storage", (e) => {
  if (e.key === key) renderSaved();
});
const example = boot.example;
if (example) {
  $("#modelExample").hidden = false;
  text(
    "#example",
    example.query +
      " — " +
      example.hypothesis +
      " " +
      example.proposal +
      " Источники: " +
      example.refs
        .map((id) => boot.evidence.find((x) => x.id === id)?.ref || id)
        .join(", ") +
      ". Полный запрос: " +
      fmt(example.seconds, 3) +
      " с, " +
      example.usage.total_tokens +
      " токена.",
  );
}
render();
renderSaved();
showHits(boot.evidence);


const memoryMode = () => $("#memoryMode").value;
if (!offline && /^Синтетическая/i.test(data.source)) $("#memoryMode").value = "synthetic";
async function refreshMemory() {
  const mode = memoryMode();
  try {
    const [overview, listing] = await Promise.all([
      api("memory/overview", {mode}), api("memory/series", {mode}),
    ]);
    if (mode !== memoryMode()) return;
    const range = overview.first_day ? overview.first_day + " — " + overview.last_day : "период не накоплен";
    text("#memoryStatus", "Источников " + overview.sources + " · наблюдений " + overview.observations +
         " · документов " + overview.documents + " · " + range);
    const lines = [];
    if (overview.jobs[0]) lines.push("Последняя синхронизация · " +
      (overview.jobs[0].finished || overview.jobs[0].started) + " · " + overview.jobs[0].status);
    if (overview.backup.status !== "unknown") {
      lines.push("Резервная копия · " + (overview.backup.last_backup || "нет подтверждения") +
        " · проверка целостности " + (overview.backup.last_integrity_check || "нет записи") +
        " · восстановление " + (overview.backup.last_restore_check || "нет записи"));
    } else lines.push("Статус резервного копирования недоступен.");
    $("#memorySources").replaceChildren(...lines.map((line) => node("p", line)));
    const select = $("#memoryCompare").elements.series;
    select.replaceChildren();
    for (const item of listing.series.filter((row) => row.aggregation !== "snapshot")) {
      const option = node("option", [item.product, item.metric, item.unit, item.segment].join(" · "));
      option.value = item.id;
      option.dataset.product = item.product;
      option.dataset.stream = item.stream;
      select.append(option);
    }
  } catch (error) {
    text("#memoryStatus", error.message);
  }
}
$("#memoryRefresh").onclick = refreshMemory;
$("#memoryMode").onchange = () => {
  $("#memoryHits").replaceChildren();
  $("#memoryResult").replaceChildren();
  refreshMemory();
};
$("#memoryCollect").onclick = async () => {
  $("#memoryCollect").disabled = true;
  try {
    const result = await api("memory/sync", {kind: "sales"});
    await refreshMemory();
    if (result.status === "error") text("#memoryStatus", "Продажи не синхронизированы. Проверьте путь к источнику.");
  } catch (error) { text("#memoryStatus", error.message); }
  finally { $("#memoryCollect").disabled = false; }
};
$("#memorySearch").onsubmit = async (event) => {
  event.preventDefault();
  const container = $("#memoryHits");
  try {
    const result = await api("memory/search", {...values(event.currentTarget), mode: memoryMode()});
    showMemoryResults(container, result);
  } catch (error) {
    container.replaceChildren(node("p", error.message));
  }
};

$("#memorySaveNote").onclick = async () => {
  const input = values($("#memorySearch"));
  if (memoryMode() !== "local") {
    text("#memoryHits", "Заметки оператора сохраняются только в рабочем режиме.");
    return;
  }
  try {
    const result = await api("memory/note", {text: input.query, product: input.product, observed: input.start});
    text("#memoryHits", "Сохранено как неподтверждённая заметка · " + result.category);
  } catch (error) { text("#memoryHits", error.message); }
};

function showMemoryResults(container, result) {
  container.replaceChildren();
  for (const item of result.documents) {
    container.append(node("p", item.reference + " · версия " + item.revision + " · " + item.content));
  }
  for (const item of result.decisions) {
    const record = node("p", [item.created.slice(0, 10), item.question, item.decision,
      item.decision_reason, item.effect].filter(Boolean).join(" · "));
    const open = node("a", "Открыть расследование");
    open.href = "#report=" + item.run_id;
    record.append(open);
    container.append(record);
  }
  if (!container.children.length) container.append(node("p", "Подходящих записей нет."));
}

function memoryComparison() {
  const input = values($("#memoryCompare"));
  return {mode: memoryMode(), series_id: Number(input.series),
    before: [input.beforeStart, input.beforeEnd], after: [input.afterStart, input.afterEnd]};
}
$("#memoryCompare").onsubmit = async (event) => {
  event.preventDefault();
  try {
    const result = await api("memory/compare", memoryComparison());
    const line = result.status === "ok"
      ? fmt(result.before.value) + " → " + fmt(result.after.value) + " · изменение " + fmt(result.delta)
      : "Окна несопоставимы или неполны · " + visibleStatus(result.before.status) + " / " + visibleStatus(result.after.status);
    $("#memoryResult").replaceChildren(node("p", line), ...result.warnings.map((warning) => node("p", warning)));
  } catch (error) { text("#memoryResult", error.message); }
};
$("#memoryAsk").onclick = () => {
  const form = $("#memoryCompare");
  if (!form.reportValidity()) return;
  const options = memoryComparison();
  if ($("#infer").disabled) { navigate("agent"); return; }
  pendingMemoryScope = {mode: options.mode, compare: options};
  $("#search").elements.query.value = "Сравни выбранные исторические периоды";
  navigate("agent");
  $("#infer").click();
};
$("#memoryChat").onclick = () => {
  if (!$("#memoryCompare").reportValidity()) return;
  const options = memoryComparison();
  window.qwenAskWithScope("Сравни выбранные исторические периоды", {memory: {mode: options.mode, compare: options}});
};
$("#memorySearchChat").onclick = () => {
  const form = $("#memorySearch");
  if (!form.reportValidity()) return;
  const {query, ...filters} = values(form);
  const search = Object.fromEntries(Object.entries(filters).filter(([, value]) => value));
  window.qwenAskWithScope(query, {memory: {mode: memoryMode(), search}});
};
function historicalProductScope() {
  const {series, afterStart, afterEnd} = $("#memoryCompare").elements;
  if (![series, afterStart, afterEnd].every((field) => field.reportValidity())) return null;
  return {series_id: Number(series.value), mode: memoryMode(), period: [afterStart.value, afterEnd.value],
    ...series.selectedOptions[0].dataset};
}
$("#memoryReleaseChat").onclick = () => {
  const scope = historicalProductScope();
  if (!scope) return;
  if (scope.stream !== "sales") {
    text("#memoryResult", "Для сравнения релизов выберите исторический ряд продаж.");
    return;
  }
  const {series_id, mode, period, product} = scope;
  window.qwenAskWithScope("Сравни последствия релизов в выбранном периоде", {memory: {
    mode, product: {product, period, series_id},
  }});
};
$("#memoryPriceChat").onclick = () => {
  const scope = historicalProductScope();
  if (!scope) return;
  const {mode, period, product} = scope;
  window.qwenAskWithScope("Как менялись цены конкурентов в выбранном периоде?", {memory: {
    mode, competitors: {product, period},
  }});
};
window.qwenCore = {
  releaseId: () => data.release_id,
  scope() {
    const scope = {config: data.config};
    if ($("#includeMeter").checked) {
      scope.metrika = $("#compareMeter").checked ? {compare: true} : values($("#meter"));
    }
    if ($("#publicProduct").value) {
      const selected = (id) => Array.from($(id).selectedOptions, (item) => item.value);
      scope.web = {product: $("#publicProduct").value, domains: selected("#publicDomains"), topics: selected("#publicTopics")};
    }
    return scope;
  },
};
document.addEventListener("qwen-backlog-updated", () => $("#reloadRuns").click());
function showPriceChanges(container, rows) {
  container.replaceChildren(...(rows.length ? rows.map((row) =>
    node("p", [row.observed, row.product, row.competitor, row.before + " → " + row.after + " " + row.currency,
      row.terms, row.reference].filter(Boolean).join(" · "))) :
    [node("p", "Сопоставимых изменений цен в этом окне нет.")]));
}
$("#memoryCompetitors").onclick = async () => {
  try {
    const input = memoryComparison();
    showPriceChanges($("#memoryResult"), await api("memory/competitors", {mode: input.mode, period: input.after}));
  } catch (error) { text("#memoryResult", error.message); }
};

for (const workspace of ["priceHistory", "releaseHistory"]) {
  const form = document.getElementById(workspace);
  form.elements.start.value = data.config.before_start;
  form.elements.end.value = data.config.after_end;
  form.onsubmit = async (event) => {
    event.preventDefault();
    const prices = workspace === "priceHistory";
    const output = $(prices ? "#priceResults" : "#releaseResults");
    const {product, start, end} = values(form);
    const input = {mode: memoryMode(), period: [start, end], ...(product.trim() ? {product: product.trim()} : {})};
    output.textContent = "Читаем базу знаний…";
    try {
      if (prices) showPriceChanges(output, await api("memory/competitors", input));
      else {
        const result = await api("memory/product", input);
        const items = result.releases.map((row) =>
          node("p", [row.date, row.id, row.metadata?.title, row.metadata?.task_id].filter(Boolean).join(" · ")));
        output.replaceChildren(...(items.length ? items : [node("p", "За этот период релизы не найдены.")]));
      }
    } catch (error) { output.textContent = error.message; }
  };
}

$("#memoryTimeline").onclick = async () => {
  try {
    const input = memoryComparison();
    const result = await api("memory/timeline", {mode: input.mode, series_id: input.series_id, period: input.after});
    const rows = result.observations.slice(-12).map((item) => node("p",
      [item.start + " — " + item.end, item.number == null ? "значение отсутствует" : fmt(item.number),
        result.series.unit, item.quality, "версия " + item.version,
        item.reference || result.series.source, "получено " + item.retrieved].join(" · ")));
    $("#memoryResult").replaceChildren(...(rows.length ? rows : [node("p", "Наблюдений за окно нет.")]));
  } catch (error) { text("#memoryResult", error.message); }
};

async function openLinkedReport(id, ref) {
  reportLink = id;
  const request = ++reportRequest;
  if (renderedReportId === id) {
    if (ref) openTarget(document.getElementById(ref));
    return;
  }
  if (offline) { flash("Откройте отчёт через локальный сервер."); return; }
  text("#runSummary", "Загрузка сохранённого отчёта…");
  try {
    const result = reportCache.get(id) || await api("analysis/report", {run_id: id});
    if (request !== reportRequest) return;
    reportCache.set(id, result);
    renderInvestigation(result);
    const currentRef = new URLSearchParams(location.hash.slice(1)).get("ref");
    if (currentRef) openTarget(document.getElementById(currentRef));
  } catch (error) {
    if (request !== reportRequest) return;
    $("#runDetails").replaceChildren();
    renderedReportId = null;
    text("#runSummary", error.message);
    reportLink = null;
  }
}
applyRoute();
if (offline) {
  text("#overviewRuns", "Журнал доступен на локальном сервере.");
  text("#runHistory", "Журнал доступен на локальном сервере.");
  text("#serverTasks", "Серверный бэклог доступен на локальном сервере.");
} else reloadRuns();
