import os, sys, subprocess, time, urllib.request, json, shutil
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

# Isolate local uploads from release data.
from tempfile import TemporaryDirectory

root = Path(__file__).resolve().parents[1]
with TemporaryDirectory() as d:
    path = Path(d)
    for item in ["pm", "web", "data", "app.py"]:
        src = root / item
        if src.is_dir():
            shutil.copytree(src, path / item)
        else:
            shutil.copy(src, path / item)
    qa_env = os.environ.copy()
    for field in (
        "PM_WORKBOOK", "PM_CASE_CONFIG", "PM_SNAPSHOT", "PM_EVIDENCE",
        "PM_BENCHMARK", "PM_LLM_URL", "PM_LLM_PROMPT_FILE",
        "METRIKA_COUNTER_ID", "METRIKA_TOKEN",
    ):
        qa_env.pop(field, None)
    qa_env["PM_PRIVATE_DIR"] = str(path / "local-data")
    flat = path / "build" / "index.html"
    subprocess.run(
        [sys.executable, "app.py", "build", "--output", str(flat)],
        cwd=path, env=qa_env, check=True,
    )
    subprocess.run(
        [sys.executable, str(root / "scripts/cost.py"), "--output", str(flat.parent / "cost.xlsx")],
        cwd=path, env=qa_env, check=True,
    )
    server = subprocess.Popen(
        [sys.executable, "app.py", "serve", "--port", "18112"], cwd=path, env=qa_env
    )
    try:
        for _ in range(50):
            try:
                with urllib.request.urlopen("http://127.0.0.1:18112/", timeout=1):
                    break
            except OSError:
                time.sleep(0.15)
        with sync_playwright() as pw:
            b = pw.chromium.launch(
                executable_path=os.getenv("PM_CHROMIUM") or None,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            ctx = b.new_context()
            t = ctx.new_page()
            errors = []
            t.on("pageerror", lambda e: errors.append(str(e)))
            t.goto("http://127.0.0.1:18112/")
            assert t.request.get("http://127.0.0.1:18112/cost.xlsx").ok
            expect(t.locator("#cards")).to_contain_text("30,0%")
            t.locator("#cards a").first.click()
            assert t.url.endswith("#dynamics")
            t.get_by_text("Числа по периодам", exact=True).click()
            expect(t.locator("#numbers")).to_contain_text("100,00")
            t.locator("#toggle").click()
            expect(t.locator("#answer")).to_be_visible()
            expect(t.locator("#reach")).to_contain_text("14/28")
            t.get_by_text("Открыть связанную задачу").click()
            assert t.url.endswith("#t08")
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
            t.locator("#mode").select_option("demo")
            expect(t.locator("#effectResult")).to_contain_text("120,00")
            t.locator("#saveDecision").click()
            expect(t.locator("#saved")).to_contain_text("ожидает проверки")
            t.locator("#mode").select_option("actual")
            expect(t.locator("#effectResult")).to_contain_text("Ожидаем")
            t.get_by_text("Загрузить новые недели").click()
            f = json.loads((root / "data/effect-demo.json").read_text())
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
            t.get_by_text("Загрузить материал в локальный поиск").click()
            t.locator("#upload [name=name]").fill("QA")
            t.locator("#upload [name=text]").fill(
                "Эквайринг покрытия: проверка карточного платежа."
            )
            t.locator("#upload button").click()
            expect(t.locator("#toast")).to_contain_text("Добавлено")
            t.locator("#search input").fill("эквайринг")
            t.locator("#search button").first.click()
            expect(t.locator("#hits")).to_contain_text("QA · абзац 1")
            t.locator("#infer").click()
            expect(t.locator("#ai")).to_contain_text("Модель не подключена")
            t.get_by_text("Настроить сравнение").click()
            t.locator("[name=after_end]").fill("2025-03-24")
            t.locator("#settings button").click()
            expect(t.locator("#period")).to_contain_text("24.03.2025")
            t.locator("#meter button").click()
            expect(t.locator("#meterResult")).to_contain_text("METRIKA_COUNTER_ID")
            mobile = b.new_context(viewport={"width": 390, "height": 844})
            m = mobile.new_page()
            m.goto("http://127.0.0.1:18112/")
            expect(m.locator("#saved")).to_contain_text("пока нет")
            assert m.evaluate("document.documentElement.scrollWidth<=window.innerWidth")
            m.locator("#toggle").click()
            expect(m.locator("#answer")).to_be_visible()
            m.goto(flat.as_uri())
            expect(m.locator("#toggle")).to_have_text("Разобрать падение")
            expect(m.locator("#cards strong").first).to_have_text("-30,0%")
            expect(m.locator("#source")).to_contain_text("Синтетическая")
            m.locator("#search input").fill("корзина")
            m.locator("#search button").first.click()
            expect(m.locator("#hits")).to_contain_text("Демо · событие корзины")
            assert not errors, errors
            print(
                "UI PASS · navigation, periods, cancel, edit, persistence, duplicate, separate browser, evidence upload/search, fallback, goal, missing API, mobile, flat snapshot"
            )
            b.close()
    finally:
        server.terminate()
        server.wait()
