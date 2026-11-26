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
const node = (tag, t) => {
  const e = document.createElement(tag);
  e.textContent = t;
  return e;
};
const text = (id, t) => {
  $(id).textContent = t;
};
const values = (f) => Object.fromEntries(new FormData(f));
const dateRu = (s) => s.split("-").reverse().join(".");
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
  if (!r.ok) throw Error(a.error || "Не удалось выполнить запрос");
  return a;
}
function drawChart() {
  const ns = "http://www.w3.org/2000/svg",
    svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 940 285");
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
  const max = Math.max(
    50,
    Math.ceil(
      Math.max(...all.flatMap((p) => p.slice(1)).filter(Number.isFinite)) / 50,
    ) * 50,
  );
  const stamps = all.map((p) => Date.parse(p[0])),
    first = Math.min(...stamps),
    last = Math.max(...stamps);
  const x = (d) => 46 + (862 * (Date.parse(d) - first)) / (last - first),
    y = (v) => 224 - (176 * v) / max;
  add("rect", {
    x: x(data.config.partial_week) - 4,
    y: 40,
    width: 12,
    height: 186,
    fill: "#f8e6ba",
  });
  for (let v = 0; v <= max; v += max / 5) {
    add("line", { x1: 46, x2: 918, y1: y(v), y2: y(v), stroke: "#dae4de" });
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
        stroke: i ? "#a4521e" : "#00678a",
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
  $("#chart").replaceChildren(svg);
}
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
  text("#source", data.source + " · недельные индексы");
  text("#release", [data.release_id, "Запуск " + dateRu(data.launch)].filter(Boolean).join(" · "));
  text("#legend", "Синий — " + data.series[0].name + ", оранжевый — " + data.series[1].name + ".");
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
  const row = (cells) => {
    const tr = node("tr", "");
    for (const v of cells) tr.append(node("td", v));
    table.append(tr);
  };
  row([
    "Продукт",
    "До",
    "После",
    "Изменение",
    "До " + dateRu(c.partial_week),
    "Изменение",
  ]);
  for (const s of data.series) {
    const a = node("a", s.name);
    a.href = "#dynamics";
    a.append(node("strong", pct(s.delta)));
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
  text(
    "#signal",
    delta != null && delta < 0 && -delta >= c.alert_percent
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
      "). В карточке релиза указана " +
      data.release_task +
      ".",
  );
  const f = $("#settings");
  for (const e of f.elements) if (e.name) e.value = c[e.name];
  drawChart();
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
    a.append(node("strong", x.kind + " · " + x.ref), node("p", x.text));
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
  f.elements.target.value = r?.target || data.series[0].rolling_base.toFixed(2);
  text(
    "#criterion",
    "Исходная база сравнения и окно после релиза: " +
      (r?.period || period(data.config)) +
      ". По умолчанию цель равна среднему четырёхнедельному индексу по базовым неделям. Подтвердите значение до исправления.",
  );
  f.scrollIntoView();
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
$("#search").onsubmit = async (e) => {
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
    text("#ai", "Результаты поиска · " + hits.length);
  } catch (err) {
    flash(err.message);
  }
};
$("#infer").onclick = async (e) => {
  e.target.disabled = true;
  const ai = $("#ai");
  ai.textContent = "Идёт подбор источников и проверок…";
  try {
    const res = await api("analyze", {
      query: $("#search").elements.query.value,
      config: data.config,
    });
    showHits(res.hits);
    proposal = res.hypothesis || res.proposal || "";
    proof = res.refs || [];
    ai.textContent = [
      res.mode,
      res.hypothesis,
      res.proposal,
      res.notice,
      res.seconds + " с",
    ]
      .filter(Boolean)
      .join(" · ");
    for (const id of proof) {
      const link = node("a", " [" + id + "]");
      link.href = "#src-" + id;
      ai.append(link);
    }
  } catch (err) {
    ai.textContent = err.message;
  } finally {
    e.target.disabled = false;
  }
};
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
        ? "Синтетический тест · "
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
            fmt(record.baseline[1].rolling) +
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
      throw Error("Нужны fix и rows");
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
            r.start +
            "–" +
            r.end +
            " · визитов " +
            r.value +
            " · получено " +
            r.retrieved +
            (r.sampled ? " · выборка" : "")
        : r.message,
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
