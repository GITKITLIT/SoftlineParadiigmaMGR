(() => {
  "use strict";
  const find = (selector) => document.querySelector(selector);
  const fab = find("#qwen-fab");
  const panel = find("#qwen-panel");
  if (!fab || !panel || location.protocol === "file:") return;

  const log = find("#qwen-log");
  const input = find("#qwen-question");
  const dialogSelect = find("#qwen-dialogs");
  const progress = find("#qwen-progress");
  const form = find("#qwen-ask");
  const mode = find("#qwen-mode");
  const newDialog = find("#qwen-new");
  const reduced = matchMedia("(prefers-reduced-motion: reduce)");
  const names = {sales: "Продажи", funnel: "Воронка", releases: "Релизы", backlog: "Бэклог",
    research: "Интервью и исследования", competitors: "Конкуренты", history: "История", metrika: "Яндекс.Метрика",
    public_competitors: "Публичные доказательства", effect: "Эффект релиза", compare_history: "Сравнение периодов",
    search_history: "Поиск в памяти", competitor_history: "Исторические цены", product_history: "История продукта"};
  const states = {ok: "готово", running: "выполняется", missing: "нет данных", partial: "частично",
    disabled: "выключен", unavailable: "недоступен", error: "ошибка", not_checked: "не проверен"};
  const phases = {decision: "Выбор источника", synthesis: "Синтез гипотез"};
  let dialogId = "", busy = false, selectedScope, opened = false, movement, skipClick = false, animation;
  let anchor;
  try { anchor = JSON.parse(localStorage.getItem("pm.chat.position")); } catch { /* browser setting */ }

  function element(tag, text) {
    const item = document.createElement(tag);
    if (text) item.textContent = text;
    return item;
  }

  async function request(path, data = {}) {
    const response = await fetch("/api/qwen/" + path, {
      method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(data),
    });
    const result = await response.json();
    if (!response.ok) throw Error(result.error || "Запрос не выполнен.");
    return result;
  }

  function place() {
    if (!anchor || !Number.isFinite(anchor.x) || !Number.isFinite(anchor.y)) return;
    fab.style.left = Math.max(8, Math.min(innerWidth - fab.offsetWidth - 8, anchor.x)) + "px";
    fab.style.top = Math.max(8, Math.min(innerHeight - fab.offsetHeight - 8, anchor.y)) + "px";
    fab.style.right = fab.style.bottom = "auto";
  }
  place();
  addEventListener("resize", place);
  fab.addEventListener("pointerdown", (event) => {
    if (event.button !== 0) return;
    const rect = fab.getBoundingClientRect();
    movement = {id: event.pointerId, x: event.clientX, y: event.clientY, dx: event.clientX - rect.left, dy: event.clientY - rect.top};
    skipClick = false;
    fab.setPointerCapture(event.pointerId);
  });
  fab.addEventListener("pointermove", (event) => {
    if (!movement || event.pointerId !== movement.id) return;
    if (!skipClick && Math.hypot(event.clientX - movement.x, event.clientY - movement.y) < 6) return;
    skipClick = true;
    anchor = {x: event.clientX - movement.dx, y: event.clientY - movement.dy};
    place();
  });
  function endDrag() {
    if (skipClick) {
      try { localStorage.setItem("pm.chat.position", JSON.stringify(anchor)); } catch { /* browser setting */ }
    }
    movement = null;
  }
  fab.addEventListener("pointerup", endDrag);
  fab.addEventListener("pointercancel", endDrag);
  fab.onclick = (event) => { if (!skipClick || event.detail === 0) open(); };
  find("#qwen-close").onclick = close;
  panel.addEventListener("keydown", (event) => { if (event.key === "Escape") close(); });

  async function open() {
    if (opened) return;
    opened = true;
    animation?.cancel();
    panel.hidden = false;
    panel.inert = false;
    fab.hidden = true;
    fab.setAttribute("aria-expanded", "true");
    animation = panel.animate([{opacity: 0, transform: "translateY(10px)"}, {opacity: 1, transform: "none"}],
      {duration: reduced.matches ? 0 : 180});
    input.focus();
    if (!dialogId && !busy) {
      lock(true);
      try {
        await refreshDialogs();
        if (dialogSelect.value) await loadDialog(dialogSelect.value);
      } catch (error) { progress.textContent = error.message; }
      finally { lock(false); }
    }
  }

  function close() {
    opened = false;
    animation?.cancel();
    panel.inert = true;
    fab.hidden = false;
    place();
    fab.setAttribute("aria-expanded", "false");
    fab.focus();
    animation = panel.animate([{opacity: 1}, {opacity: 0, transform: "translateY(10px)"}],
      {duration: reduced.matches ? 0 : 135});
    animation.onfinish = () => { if (!opened) panel.hidden = true; };
  }

  function lock(value) {
    busy = value;
    dialogSelect.disabled = newDialog.disabled = form.querySelector("button").disabled = value;
  }

  async function refreshDialogs() {
    const {dialogs} = await request("dialogs");
    dialogSelect.replaceChildren(...dialogs.map((dialog) => {
      const option = element("option", dialog.label?.slice(0, 70) || "Новый диалог");
      option.value = dialog.id;
      return option;
    }));
    if (dialogId) dialogSelect.value = dialogId;
  }

  async function loadDialog(identifier) {
    const {turns} = await request("history", {dialog_id: identifier});
    dialogId = identifier;
    dialogSelect.value = identifier;
    log.replaceChildren();
    for (const turn of turns) log.append(turnBlock(turn.question, turn.answer, turn.position));
    log.scrollTop = log.scrollHeight;
  }

  async function createDialog() {
    const result = await request("new");
    dialogId = result.dialog_id;
    log.replaceChildren();
    await refreshDialogs();
  }

  newDialog.onclick = async () => {
    lock(true);
    try { await createDialog(); progress.textContent = ""; }
    catch (error) { progress.textContent = error.message; }
    finally { lock(false); input.focus(); }
  };

  dialogSelect.onchange = async () => {
    lock(true);
    try { await loadDialog(dialogSelect.value); progress.textContent = ""; }
    catch (error) { progress.textContent = error.message; }
    finally { lock(false); }
  };

  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      if (!busy) form.requestSubmit();
    }
  });

  function details(label, children) {
    const block = element("details");
    block.append(element("summary", label), ...children);
    return block;
  }

  function cite(view, ids) {
    const found = view.sources.filter((item) => ids.includes(item.id));
    return found.length ? details("Источники", found.map(source)) : element("span");
  }

  function source(item) {
    const row = element("div");
    row.append(element("strong", item.ref));
    const origin = [item.id, item.quality, item.data_mode, item.observed_at, item.freshness].filter(Boolean);
    row.append(element("p", origin.join(" · ")));
    if (item.text) row.append(element("p", item.text));
    if (item.values) row.append(element("p", Object.entries(item.values).map(([key, value]) =>
      key + " · " + (value == null ? "нет данных" : JSON.stringify(value))).join(" / ")));
    if (item.kind === "competitor_web" && typeof item.locator === "string") {
      const labels = {product: "Продукт", version: "Редакция", license_type: "Тип лицензии", duration: "Срок",
        devices: "Устройства", currency: "Валюта", tax: "Налоги", price: "Цена"};
      const terms = Object.entries(labels).filter(([key]) => item.attributes?.[key]).map(([key, label]) =>
        label + " · " + item.attributes[key]);
      if (terms.length) row.append(element("p", terms.join(" / ")));
      if (item.quality === "snippet") row.append(element("p", "Поисковый фрагмент. Условия требуют проверки страницы."));
      try {
        const url = new URL(item.locator);
        if (["https:", "http:"].includes(url.protocol)) {
          const link = element("a", "Открыть страницу");
          link.href = url.href;
          link.target = "_blank";
          link.rel = "noopener noreferrer";
          row.append(link);
        }
      } catch { /* source without valid URL */ }
    }
    return row;
  }

  function renderView(view, position) {
    const block = element("div");
    block.append(element("p", view.answer));
    if (view.next_step) block.append(element("p", "Следующий шаг · " + view.next_step));
    if (view.notice) block.append(element("p", view.notice));
    if (!view.sources) return block;
    if (view.data_mode === "synthetic") block.append(element("p", "Синтетические данные"));
    if (view.previous_run_id) block.append(element("p", "Продолжение предыдущего расследования"));
    if (view.measurements.length || view.observations.length) {
      block.append(details("Наблюдения", [
        ...view.measurements.map((item) => {
          const row = element("div");
          row.append(element("p", item.text), cite(view, [item.evidence_id]));
          return row;
        }), ...view.observations.map(source),
      ]));
    }
    if (view.hypotheses.length) {
      block.append(details("Гипотезы", view.hypotheses.map((item) => {
        const row = element("div");
        row.append(element("strong", item.cause), element("p", item.verify), element("p", item.expected),
          element("p", "Нужны данные · " + item.missing), element("p", "Альтернативы · " + item.alternative_explanations.join(" / ")),
          cite(view, item.evidence_ids));
        if (item.contradictions.length) row.append(details("Противоречия", [cite(view, item.contradictions)]));
        row.append(element("p", "Уверенность · " + (item.confidence === "medium" ? "средняя" : "низкая")));
        return row;
      })));
    }
    if (view.trace.length) {
      block.append(details("Шаги расследования", view.trace.filter((item) => item.step === "tool").map((item) => {
        const row = element("div");
        row.append(element("p", [names[item.tool] || item.tool, states[item.status] || item.status,
          item.actor === "qwen" ? "выбор Qwen" : "серверная проверка"].join(" · ")), cite(view, item.evidence_ids));
        return row;
      })));
    }
    const flagged = Object.entries(view.sources_status).filter(([, item]) => ["missing", "partial", "unavailable", "error"].includes(item.status));
    if (view.limitations.length || flagged.length) {
      block.append(details("Ограничения", [...view.limitations.map((line) => element("p", line)),
        ...flagged.map(([tool, item]) => element("p", (names[tool] || tool) + " · " + states[item.status]))]));
    }
    if (position && view.drafts.length) block.append(details("Черновики задач",
      view.drafts.map((draft) => draftForm(view, position, draft))));
    return block;
  }

  function draftForm(view, position, draft) {
    const box = element("form");
    const fields = {};
    const submit = element("button", "Подтвердить сохранение в бэклоге");
    const status = element("p");
    status.setAttribute("role", "status");
    for (const [label, key, length] of [["Название", "title", 200], ["Гипотеза", "hypothesis", 800],
      ["Способ проверки", "verification", 1200]]) {
      const wrapper = element("label", label);
      const field = element("textarea");
      field.value = draft[key];
      field.required = true;
      field.maxLength = length;
      field.rows = 2;
      fields[key] = field;
      wrapper.append(field);
      box.append(wrapper);
    }
    const label = element("label", "Цель четырёхнедельного индекса");
    const target = element("input");
    Object.assign(target, {type: "number", min: "0.01", step: "any", required: true, value: draft.plan.target ?? ""});
    label.append(target);
    box.append(cite(view, draft.evidence_ids), element("p", "Критерий · " + draft.criteria.join(" / ")),
      element("p", "Ожидаемое наблюдение · " + draft.expected),
      element("p", "Альтернативы · " + draft.alternatives.join(" / ")),
      element("p", "Нужны данные · " + draft.priority.dependencies.join(" / ")),
      element("p", "База · " + (draft.plan.baseline_period?.join(" — ") || "нет периода")),
      element("p", "Контроль · " + (draft.plan.guardrails.join(" / ") || "нет контрольного ряда")),
      element("p", draft.priority.reason), element("p", draft.tag), label, submit, status);
    if (draft.existing_tasks.length) {
      box.prepend(element("p", "Пересмотреть приоритет · " + draft.existing_tasks.map((item) => item.id + " " + item.title).join(" / ")));
    }
    if (!draft.backlog_verified || draft.plan.baseline == null) {
      submit.disabled = true;
      status.textContent = "Для согласования нужны проверенный бэклог и базовый уровень.";
    }
    box.onsubmit = async (event) => {
      event.preventDefault();
      submit.disabled = true;
      try {
        await request("approve", {dialog_id: dialogId, position, proposal_id: draft.id, confirm: true,
          target: Number(target.value), draft: Object.fromEntries(Object.entries(fields).map(([key, field]) => [key, field.value]))});
        status.textContent = "Сохранено в серверном бэклоге.";
        document.dispatchEvent(new Event("qwen-backlog-updated"));
      } catch (error) { status.textContent = error.message; submit.disabled = false; }
    };
    return box;
  }

  function turnBlock(question, answer, position, view) {
    const wrapper = element("article");
    const prompt = element("p", question);
    prompt.className = "qwen-user";
    wrapper.append(prompt);
    if (view) wrapper.append(renderView(view, position));
    else {
      const button = element("button", "Раскрыть ответ");
      wrapper.append(element("p", answer), button);
      button.onclick = async () => {
        button.disabled = true;
        try {
          const reply = await request("turn", {dialog_id: dialogId, position});
          wrapper.replaceChildren(prompt, renderView(reply, position));
        } catch (error) { button.textContent = error.message; button.disabled = false; }
      };
    }
    return wrapper;
  }

  form.onsubmit = async (event) => {
    event.preventDefault();
    if (busy) return;
    const question = input.value.trim();
    if (!question) return;
    lock(true);
    progress.textContent = "Запускаем расследование…";
    try {
      if (!dialogId) await createDialog();
      const scope = window.qwenCore.scope();
      const {job_id} = await request("start", {dialog_id: dialogId, question, mode: mode.value,
        scope: {...scope, ...selectedScope}});
      const deadline = Date.now() + 210000;
      while (Date.now() < deadline) {
        const state = await request("status", {dialog_id: dialogId, job_id});
        if (state.status === "ready") {
          log.append(turnBlock(question, state.reply.answer, state.position, state.reply));
          log.scrollTop = log.scrollHeight;
          progress.textContent = "";
          input.value = "";
          mode.value = "auto";
          selectedScope = undefined;
          await refreshDialogs();
          return;
        }
        if (state.status === "failed") throw Error("Расследование завершилось с ошибкой. Проверьте область и источники.");
        const phase = state.phases.at(-1);
        if (phase) progress.textContent = [names[phase.tool] || phases[phase.step], states[phase.status] || phase.status].filter(Boolean).join(" · ");
        await new Promise((resolve) => setTimeout(resolve, 650));
      }
      throw Error("Время ожидания истекло. Обновите диалог, чтобы проверить, сохранился ли ответ.");
    } catch (error) { progress.textContent = error.message; }
    finally { lock(false); if (opened) input.focus(); }
  };

  window.qwenAskWithScope = (question, scope) => {
    if (busy) { progress.textContent = "Дождитесь текущего ответа."; return; }
    input.value = question;
    selectedScope = scope;
    mode.value = "new_topic";
    open();
  };

  const historyButton = find("#memoryChat");
  if (historyButton) historyButton.hidden = false;
  let lastReleaseRun = null;
  const release = find("#qwen-release");
  function renderRelease(view) {
    release.replaceChildren(element("h3", "Выжимка по релизу"), renderView(view));
    release.hidden = false;
  }
  if (release && window.qwenCore.releaseId()) {
    request("brief-latest", {release_id: window.qwenCore.releaseId()}).then(({brief}) => {
      if (!lastReleaseRun && brief) renderRelease(brief);
    }).catch(() => { /* Core overview remains visible */ });
  }
  document.addEventListener("qwen-release-report", async (event) => {
    const runId = event.detail;
    const release = find("#qwen-release");
    if (!release) return;
    lastReleaseRun = runId;
    if (!runId) { release.hidden = true; return; }
    try {
      const view = await request("brief", {run_id: runId});
      if (lastReleaseRun !== runId) return;
      renderRelease(view);
    } catch (error) {
      if (lastReleaseRun !== runId) return;
      release.textContent = error.message;
      release.hidden = false;
    }
  });
})();
