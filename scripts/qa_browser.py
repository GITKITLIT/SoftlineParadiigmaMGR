import os, sys, subprocess, time, urllib.request, json, shutil, re, socket
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

# Isolate local uploads from release data.
from tempfile import TemporaryDirectory

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
from scripts.deploy.render_proxy import render as render_proxy
with TemporaryDirectory() as d:
    path = Path(d) / "project"
    path.mkdir()
    for item in ["pm", "web", "data", "app.py", "llm_lab"]:
        src = root / item
        if src.is_dir():
            shutil.copytree(src, path / item)
        else:
            shutil.copy(src, path / item)
    qa_env = {key: value for key, value in os.environ.items()
              if not key.startswith(("PM_", "METRIKA_", "YANDEX_SEARCH_"))}
    qa_env.update(PM_PRIVATE_DIR=str(Path(d) / "local-data"), PM_QWEN_CHAT_ENABLED="true",
                  PM_QWEN_OWNER_MODE="local")
    flat = path / "build" / "index.html"
    subprocess.run(
        [sys.executable, str(path / "app.py"), "build", "--output", str(flat)],
        cwd=root, env=qa_env, check=True,
    )
    subprocess.run(
        [sys.executable, str(root / "scripts/cost.py"), "--output", str(flat.parent / "cost.xlsx")],
        cwd=root, env=qa_env, check=True,
    )
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    base = f"http://127.0.0.1:{port}/"
    server = subprocess.Popen(
        [sys.executable, str(path / "app.py"), "serve", "--port", str(port)], cwd=root, env=qa_env
    )
    try:
        for _ in range(50):
            try:
                with urllib.request.urlopen(base, timeout=1):
                    break
            except OSError:
                time.sleep(0.15)
        else:
            raise RuntimeError("The isolated Dashboard did not start")
        with sync_playwright() as pw:
            b = pw.chromium.launch(
                executable_path=os.getenv("PM_CHROMIUM") or None,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            ctx = b.new_context(viewport={"width": 1440, "height": 1000}, reduced_motion="reduce")
            t = ctx.new_page()
            errors = []
            t.on("pageerror", lambda e: errors.append(str(e)))
            t.on("console", lambda msg: errors.append(msg.text) if "Content Security Policy" in msg.text else None)
            proxy = render_proxy("acceptance.example.org", "production", qwen_chat=True)
            csp = re.search(r'add_header Content-Security-Policy "([^"]+)"', proxy)[1]
            def enforce_csp(route):
                response = route.fetch()
                route.fulfill(response=response, headers={**response.headers, "Content-Security-Policy": csp})
            t.route(base, enforce_csp)
            t.goto(base)

            def section(key, page=t):
                if page.locator("#mobileMenu").is_visible():
                    page.locator("#mobileMenu").click()
                page.locator(f"[data-nav={key}]").click()
                expect(page.locator(".workspace:visible")).to_have_count(1)
                expect(page.locator(f"[data-workspace][id={key}]")).to_be_visible()

            sections = ("overview", "agent", "qwen", "dynamics", "competitors", "sources", "releases",
                        "backlog", "effect", "productMemory", "journal", "settingsPanel")
            for key in sections:
                section(key)
            section("overview")
            group = t.locator(".nav-group-toggle").nth(1)
            group.click()
            expect(group).to_have_attribute("aria-expanded", "false")
            expect(t.locator("[data-nav=agent]")).to_be_visible()
            expect(t.locator("[data-nav=dynamics]")).to_be_hidden()
            t.locator("#sidebarToggle").click()
            expect(t.locator("#sidebarToggle")).to_have_attribute("aria-expanded", "false")
            expect(t.locator("[data-nav=dynamics]")).to_be_visible()
            t.reload()
            expect(t.locator("#sidebarToggle")).to_have_attribute("aria-expanded", "false")
            t.locator("#sidebarToggle").click()
            expect(group).to_have_attribute("aria-expanded", "false")
            group.click()

            assert t.request.get(f"{base}cost.xlsx").ok
            expect(t.locator("#cards")).to_contain_text("30,0%")
            t.locator("#cards a").first.click()
            assert t.url.endswith("#dynamics")
            t.get_by_text("Числа по периодам", exact=True).click()
            expect(t.locator("#numbers")).to_contain_text("100,00")
            section("releases")
            t.locator("#toggle").click()
            expect(t.locator("#answer")).to_be_visible()
            expect(t.locator("#reach")).to_contain_text("14/28")
            t.get_by_text("Открыть связанную задачу").click()
            assert t.url.endswith("#t08")
            section("backlog")
            t.locator("#draftBtn").click()
            t.locator("#cancel").click()
            expect(t.locator("#saved")).to_contain_text("пока нет")
            t.locator("#draftBtn").click()
            t.locator("[name=title]").fill("Приёмка корзины")
            t.locator("#task button").first.click()
            t.reload()
            expect(t.locator("#saved")).to_contain_text("Приёмка корзины")
            expect(t.locator("#saved")).to_contain_text("сгенерирована ИИ-помощником")
            t.locator("#draftBtn").click()
            t.locator("#task button").first.click()
            assert (
                t.evaluate(
                    'Object.keys(localStorage).filter(k=>k.includes(".task.")).length'
                )
                == 1
            )
            section("effect")
            t.locator("#mode").select_option("demo")
            expect(t.locator("#effectResult")).to_contain_text("120,00")
            t.locator("#saveDecision").click()
            section("backlog")
            expect(t.locator("#saved")).to_contain_text("ожидает проверки")
            section("effect")
            t.locator("#mode").select_option("actual")
            expect(t.locator("#effectResult")).to_contain_text("Ожидаем")
            t.get_by_text("Загрузить новые недели").click()
            f = json.loads((root / "data/effect-demo.json").read_text(encoding="utf-8"))
            f["rows"][-1]["kaspersky"] = 20
            t.locator("#weeksFile").set_input_files(
                {
                    "name": "qa.json",
                    "mimeType": "application/json",
                    "buffer": json.dumps(f).encode(),
                }
            )
            expect(t.locator("#effectResult")).to_contain_text("Пересмотреть гипотезу")
            t.locator("#decision").select_option("Пересмотреть гипотезу")
            t.locator("#saveDecision").click()
            t.reload()
            expect(t.locator("#saved")).to_contain_text(
                "Решение: Пересмотреть гипотезу"
            )
            section("sources")
            t.get_by_text("Загрузить материал в локальный поиск").click()
            t.locator("#upload [name=name]").fill("QA")
            t.locator("#upload [name=text]").fill(
                "Эквайринг покрытия: проверка карточного платежа."
            )
            t.locator("#upload button").click()
            expect(t.locator("#toast")).to_contain_text("Добавлено")
            t.locator("#researchSearch input").fill("эквайринг")
            t.locator("#researchSearch button").first.click()
            expect(t.locator("#hits")).to_contain_text("QA · абзац 1")
            t.locator("#investigateResearch").click()
            expect(t.locator("#search input")).to_have_value("эквайринг")
            t.locator("#search input").fill("Почему снизились заказы? Проверь воронку, интервью, конкурентов и бэклог.")
            held = []
            def hold_first_status(route):
                if not held:
                    held.append(True)
                    route.fulfill(json={"status": "running", "phases": [{"step": "queue", "status": "waiting"}]})
                else:
                    route.continue_()
            t.route("**/api/analysis/status", hold_first_status)
            t.locator("#infer").click()
            section("overview")
            expect(t.locator("#activeInvestigation")).to_be_visible()
            expect(t.locator("#overviewRuns .report-row").first).to_be_visible(timeout=12000)
            t.locator("#overviewRuns .report-row").first.click()
            expect(t.locator("#ai")).to_contain_text("Доступен аналитический режим")
            expect(t.locator("#runDetails")).to_contain_text("Доступность источников")
            expect(t.locator("#runDetails")).to_contain_text("Действия агента")
            expect(t.locator("#runDetails")).to_contain_text("Гипотезы по правилам")
            proposal = t.locator("#runDetails form").first
            proposal.get_by_label("Целевой индекс").fill("110")
            proposal.get_by_role("button", name="Согласовать").click()
            expect(proposal).to_contain_text("Задача согласована")
            assert "#report=" in t.url
            report_url = t.url
            t.reload()
            expect(t.locator("#runDetails")).to_contain_text("Гипотезы по правилам")
            section("journal")
            t.locator("#reloadRuns").click()
            section("backlog")
            expect(t.locator("#serverTasks")).to_contain_text("согласована")
            with t.expect_download() as exported:
                t.get_by_role("button", name="Экспорт Redmine").click()
            assert "сгенерирована ИИ-помощником" in exported.value.path().read_text(encoding="utf-8")
            section("journal")
            t.locator("#dailyDigest").click()
            expect(t.locator("#digestStatus")).to_contain_text("Синтетические · расследований 1")
            section("productMemory")
            t.locator("#memoryMode").select_option("synthetic")
            expect(t.locator("#memoryStatus")).to_contain_text("Источников")
            expect(t.locator("#memoryCompare select option").first).to_contain_text("orders_index")
            for field, value in {
                "beforeStart": "2025-02-03", "beforeEnd": "2025-02-09",
                "afterStart": "2025-03-10", "afterEnd": "2025-03-16",
            }.items():
                t.locator(f"#memoryCompare input[name={field}]").fill(value)
            t.get_by_role("button", name="Сравнить периоды", exact=True).click()
            expect(t.locator("#memoryResult")).to_contain_text("изменение")
            section("backlog")
            t.locator("#serverTasks input").first.set_input_files({
                "name": "simulation.json", "mimeType": "application/json",
                "buffer": (root / "data/effect-demo.json").read_bytes(),
            })
            expect(t.locator("#serverTasks")).to_contain_text("Симуляция")
            section("dynamics")
            t.get_by_text("Настроить сравнение").click()
            t.locator("[name=after_end]").fill("2025-03-24")
            t.locator("#settings button").click()
            expect(t.locator("#period")).to_contain_text("24.03.2025")
            t.locator("#meter button").click()
            expect(t.locator("#meterResult")).to_contain_text("Метрика недоступна")
            section("competitors")
            t.locator("#priceHistory button").click()
            expect(t.locator("#priceResults")).to_contain_text("Сопоставимых")
            section("releases")
            t.locator("#releaseHistory [name=product]").fill("Лицензии A")
            t.locator("#releaseHistory button").click()
            expect(t.locator("#releaseResults")).not_to_contain_text("Читаем")
            section("settingsPanel")
            expect(t.locator("#connectionDetails")).to_contain_text("Отвечает")
            section("qwen")
            t.locator("#qwen-question").fill("Несохранённый черновик")
            section("overview")
            t.evaluate("window.dashboardNavigationMarker = 1")
            t.go_back()
            expect(t.locator("#qwen-question")).to_have_value("Несохранённый черновик")
            t.go_forward()
            expect(t.locator("#overview")).to_be_visible()
            assert t.evaluate("window.dashboardNavigationMarker") == 1
            section("qwen")
            t.locator("#qwen-question").fill("Что известно о воронке?")
            t.locator("#qwen-ask").get_by_role("button", name="Спросить").click()
            section("journal")
            section("qwen")
            expect(t.locator("#qwen-log")).to_contain_text("Что известно о воронке?", timeout=12000)
            t.goto(report_url)
            expect(t.locator("#runDetails")).to_contain_text("Гипотезы по правилам")
            citation = t.locator('#runDetails details[open] a[href*="&ref=evidence-"]').first
            citation.click()
            assert "&ref=evidence-" in t.url
            assert t.evaluate("document.activeElement.id.startsWith('evidence-')")
            t.reload()
            expect(t.locator("#runDetails")).to_contain_text("Гипотезы по правилам")
            assert t.evaluate("document.activeElement.id.startsWith('evidence-')")
            output = os.getenv("PM_DASHBOARD_SCREENSHOTS")
            if output:
                screenshots = Path(output)
                screenshots.mkdir(parents=True, exist_ok=True)
                for index, key in enumerate(sections, 1):
                    section(key)
                    if key == "qwen":
                        expect(t.locator("#qwen-log")).to_contain_text("Что известно о воронке?")
                    t.screenshot(path=screenshots / f"{index:02d}-{key}.png", full_page=True)
            for width, height in ((1024, 820), (768, 1024), (390, 844), (320, 720)):
                responsive = b.new_context(viewport={"width": width, "height": height}, reduced_motion="reduce")
                layout = responsive.new_page()
                layout.goto(base)
                for key in sections:
                    section(key, layout)
                    assert layout.evaluate("document.documentElement.scrollWidth <= innerWidth"), (key, width)
                if output and width == 390:
                    section("overview", layout)
                    layout.screenshot(path=screenshots / "13-mobile-overview.png", full_page=True)
                    layout.locator("#mobileMenu").click()
                    layout.screenshot(path=screenshots / "14-mobile-menu.png")
                responsive.close()
            mobile = b.new_context(viewport={"width": 390, "height": 844})
            m = mobile.new_page()
            m.goto(base)
            section("backlog", m)
            expect(m.locator("#saved")).to_contain_text("пока нет")
            assert m.evaluate("document.documentElement.scrollWidth<=window.innerWidth")
            section("releases", m)
            m.locator("#toggle").click()
            expect(m.locator("#answer")).to_be_visible()
            m.goto(flat.as_uri())
            expect(m.locator("#cards strong").first).to_have_text("-30,0%")
            expect(m.locator("#source")).to_contain_text("Демонстрационный режим")
            section("sources", m)
            m.locator("#researchSearch input").fill("корзина")
            m.locator("#researchSearch button").first.click()
            expect(m.locator("#hits")).to_contain_text("Демо · событие корзины")
            for key in sections:
                section(key)
                expect(t.locator("[data-workspace]:visible")).not_to_contain_text(re.compile(r"qwen", re.I))
                assert not re.search(r"qwen", t.title(), re.I)
            section("overview")
            expect(t.locator("#connectionStatus")).to_contain_text("Демонстрационный режим")
            expect(m.locator("#source")).to_contain_text("сохранённый отчёт")
            # Exercise the working label with a synthetic response and isolated storage.
            def working_label(route):
                response = route.fetch()
                body = response.text()
                assert "Синтетическая" in body
                route.fulfill(response=response, body=body.replace("Синтетическая", "Тестовая", 1))

            working = ctx.new_page()
            working.route(base, working_label)
            working.goto(base)
            expect(working.locator("#connectionStatus")).to_contain_text("Рабочий режим")
            working.close()
            if output:
                t.emulate_media(media="print")
                assert t.locator(".topbar").evaluate("(item) => getComputedStyle(item).position") == "static"
                t.pdf(path=screenshots / "15-overview-print.pdf", format="A4", print_background=True)
            assert not errors, errors
            print(
                "UI PASS · navigation, periods, cancel, edit, persistence, duplicate, separate browser, evidence upload/search, fallback, goal, missing API, mobile, flat snapshot"
            )
            b.close()
    finally:
        if server.poll() is None:
            if os.name == "nt":
                # The Windows virtualenv launcher can spawn a second Python process.
                subprocess.run(
                    ["taskkill.exe", "/PID", str(server.pid), "/T", "/F"],
                    capture_output=True, check=True,
                )
            else:
                server.terminate()
        server.wait(timeout=10)
