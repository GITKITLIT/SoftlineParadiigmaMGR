"""Local-only rehearsal using the pinned product and its synthetic fixtures."""
import argparse
from contextlib import ExitStack, contextmanager
from email import message_from_bytes, policy
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
from socketserver import TCPServer
import ssl
import subprocess
import sys
import tempfile
from threading import Thread
from time import monotonic, sleep
from urllib.request import ProxyHandler, build_opener

SOURCE = Path(__file__).resolve().parents[1]
BASE = Path(tempfile.gettempdir()) / "ParadiigmaMGR-public-demo"
sys.path[:0] = [str(SOURCE), str(SOURCE / "tests")]
import app
from llm_lab.integration import chat_api
from pm import ai_store, history, memory, monitor
from scripts.pim_acceptance import point
from test_email_notifications import SmtpHandler

QUESTION = "Почему снизились заказы? Проверь воронку, интервью, конкурентов и бэклог."
FOLLOW_UP = "Как оценить эффект?"
TITLE = "Сверить наличие позиции в корзине и регистрацию события"
PRODUCT = "Демо · история заказов"
BEFORE = "2024-05-08"
AFTER = "2025-05-08"


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf8")


def configure(run, *, live):
    for name in tuple(os.environ):
        if name.startswith(("PM_", "METRIKA_", "YANDEX_SEARCH_")):
            os.environ.pop(name)
    os.environ.update(PM_PRIVATE_DIR=str(run / "db"), PM_QWEN_CHAT_ENABLED="true", PM_QWEN_OWNER_MODE="local")
    if live:
        with build_opener(ProxyHandler({})).open("http://127.0.0.1:18081/health", timeout=5) as response:
            if json.load(response).get("status") != "ok":
                raise RuntimeError("Дождитесь готовности утверждённой Qwen3-4B.")
        prompt = run / "synthesis-grounded.ru.txt"
        prompt.write_bytes((SOURCE / "llm_lab/prompts/synthesis-grounded.ru.txt").read_bytes())
        os.environ.update(PM_LLM_URL="http://127.0.0.1:18081/v1", PM_LLM_MODEL="qwen3-4b",
                          PM_LLM_SELECT_STEP="true", PM_QWEN_QUALITATIVE="true", PM_LLM_PROMPT_FILE=str(prompt))


@contextmanager
def background(server):
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield
    finally:
        server.shutdown()
        thread.join()


class CaptureHandler(SmtpHandler):
    def handle(self):
        count = len(self.server.messages)
        super().handle()
        if len(self.server.messages) > count:
            (self.server.run / "notification.eml").write_bytes(self.server.messages[-1])


def relay(stack, run, site):
    cert = run / "loopback-cert.pem"
    key = run / "loopback-key.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
                    "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost",
                    "-out", str(cert), "-keyout", str(key)], check=True, capture_output=True)
    server = stack.enter_context(TCPServer(("127.0.0.1", 0), CaptureHandler))
    server.run = run
    server.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.context.load_cert_chain(cert, key)
    key.unlink()
    server.implicit = server.reject_auth = server.disconnect_after_data = False
    server.recipients, server.messages, server.authenticated_over_tls = [], [], []
    server.data_reply = b"250 captured on loopback\r\n"
    stack.enter_context(background(server))
    os.environ.update(SSL_CERT_FILE=str(cert), PM_EMAIL_ENABLED="true", PM_EMAIL_TEST_MODE="true",
                      PM_SMTP_HOST="localhost", PM_SMTP_PORT=str(server.server_address[1]),
                      PM_SMTP_SECURITY="starttls", PM_EMAIL_FROM="demo-sender@example.invalid",
                      PM_EMAIL_TO="demo-recipient@example.invalid", PM_EMAIL_REPORT_BASE_URL=site,
                      PM_EMAIL_DAILY_LIMIT="1", PM_MONITOR_INTERVAL="60")
    return server


def wait_for_report():
    started = monotonic()
    while monotonic() - started < 350:
        rows = ai_store.notifications()
        if rows and rows[0]["email_status"] == "submitted":
            report = ai_store.run(rows[0]["run_id"])
            if report["llm_status"] != "ok" or not report["hypotheses"]:
                raise RuntimeError(f"Фоновый отчёт получен с llm_status={report['llm_status']}.")
            return rows[0], report
        if rows and (rows[0]["status"] == "failed" or rows[0]["email_status"] in {"failed", "uncertain"}):
            raise RuntimeError("Фоновое расследование или SMTP завершились ошибкой.")
        sleep(1)
    raise RuntimeError("Истёк срок ожидания фонового отчёта и SMTP.")


def post(browser, site, route, body):
    response = browser.request.post(site + "api/" + route, data=body)
    if not response.ok:
        raise RuntimeError(f"HTTP {response.status} для {route}")
    return response.json()


def keep_browser_session(browser, run, site):
    write_json(run / "browser-session.json", browser.cookies(site + "api/qwen/history"))


def restore_browser_session(browser, run):
    cookie_file = run / "browser-session.json"
    if cookie_file.is_file():
        browser.add_cookies(json.loads(cookie_file.read_text(encoding="utf8")))


def ask(page, question, turn):
    page.locator("#qwen-question").fill(question)
    page.locator("#qwen-ask button").click()
    page.locator(f"#qwen-turn-{turn}").wait_for(timeout=265_000)
    return page.locator(f"#qwen-turn-{turn}")


def exercise(run, site, smtp):
    from playwright.sync_api import expect, sync_playwright
    notice, report = wait_for_report()
    model_tools = [row["tool"] for row in report["trace"] if row["step"] == "tool" and row["actor"] == "qwen"]
    assert model_tools
    assert len(smtp.messages) == 1 and smtp.recipients == ["<demo-recipient@example.invalid>"]
    message = message_from_bytes(smtp.messages[0], policy=policy.default)
    link = site + "#report=" + report["run_id"]
    assert link in message.get_content()
    print("Live monitor report and local smtp submitted", flush=True)
    write_json(run / "investigation.json", report)
    with sync_playwright() as play:
        browser = play.chromium.launch_persistent_context(run / "chrome-profile", channel="chrome",
                                                          headless=True, viewport={"width": 1400, "height": 950})
        page = browser.pages[0]
        page.goto(link)
        expect(page.locator("#runDetails")).to_contain_text("Гипотезы Qwen")
        page.locator("#runDetails").screenshot(path=run / "report.png")
        page.locator("#qwen-fab").click()
        first = ask(page, QUESTION, 1)
        keep_browser_session(browser, run, site)
        dialog = page.locator("#qwen-dialogs").input_value()
        reply = post(browser, site, "qwen/turn", {"dialog_id": dialog, "position": 1})
        assert reply["llm_status"] == "ok" and reply["hypotheses"]
        page.locator("#qwen-mode").select_option("follow_up")
        follow_up = ask(page, FOLLOW_UP, 2)
        expect(follow_up.get_by_role("link", name="Предыдущий вопрос")).to_have_attribute("href", "#qwen-turn-1")
        expect(follow_up.get_by_text("Как измерить эффект", exact=True)).to_be_visible()
        saved_plan = post(browser, site, "qwen/turn", {"dialog_id": dialog, "position": 2})
        assert saved_plan["effect_plan"] == reply["effect_plan"] and not saved_plan["usage"]
        first.get_by_text("Черновики задач", exact=True).click()
        draft = first.locator("form").first
        draft.get_by_label("Название").fill(TITLE)
        draft.get_by_label("Способ проверки").fill("Сверить видимую позицию в корзине с журналом событий.")
        draft.get_by_label("Цель четырёхнедельного индекса").fill("110")
        with page.expect_response(lambda item: item.url.endswith("/api/qwen/approve")) as approval:
            draft.get_by_role("button", name="Подтвердить сохранение в бэклоге").click()
        assert approval.value.ok, approval.value.status
        task = approval.value.json()
        assert task["tag"] == ai_store.TASK_TAG and task["title"] == TITLE
        assert task["effect_plan"]["observation_weeks"] == 4 and task["effect_plan"]["target"] == 110
        draft.screenshot(path=run / "approval.png")
        page.locator("#qwen-close").click()
        page.locator("#journal summary").click()
        expect(page.locator("#runHistory")).to_contain_text("почта принято SMTP")
        export = post(browser, site, "tasks/redmine-export", {"task_id": task["id"]})
        assert export["tag"] == ai_store.TASK_TAG
        write_json(run / "redmine.json", export)
        show_simulation(run, page)
        complete_history(run, site, browser, page, dialog)
        browser.close()
    assert len(smtp.messages) == 1


def show_simulation(run, page):
    from playwright.sync_api import expect

    row = page.locator("#serverTasks article").filter(has_text=TITLE)
    expect(row).to_contain_text(ai_store.TASK_TAG)
    with page.expect_response(lambda item: item.url.endswith("/api/tasks/review")) as review:
        row.locator("input[type=file]").set_input_files(str(SOURCE / "data/effect-demo.json"))
    effect = review.value.json()
    assert effect["status"] == "achieved" and effect["mode"] == "simulation"
    write_json(run / "simulation.json", effect)
    expect(row).to_contain_text("Симуляция")
    row.screenshot(path=run / "backlog-effect.png")


def complete_history(run, site, browser, page, dialog):
    from playwright.sync_api import expect

    report = json.loads((run / "investigation.json").read_text(encoding="utf8"))
    export = json.loads((run / "redmine.json").read_text(encoding="utf8"))
    effect = json.loads((run / "simulation.json").read_text(encoding="utf8"))
    notice = next(row for row in ai_store.notifications() if row["run_id"] == report["run_id"])
    task = next(row for row in ai_store.task_list() if row["id"] == export["local_task_id"])
    assert notice["email_status"] == "submitted" and notice["email_attempts"] == 1
    assert task["tag"] == ai_store.TASK_TAG and task["title"] == TITLE
    assert task["effect_plan"]["target"] == 110 and effect["mode"] == "simulation" and effect["status"] == "achieved"
    previous = post(browser, site, "qwen/turn", {"dialog_id": dialog, "position": 1})
    plan = post(browser, site, "qwen/turn", {"dialog_id": dialog, "position": 2})
    assert previous["llm_status"] == "ok" and plan["previous_position"] == 1
    assert plan["effect_plan"] == previous["effect_plan"] and not plan["usage"]
    page.locator("#memoryMode").select_option("synthetic")
    compare = page.locator("#memoryCompare")
    selected = history.series_index("synthetic", product=PRODUCT)[0]
    compare.get_by_role("combobox").select_option(value=str(selected["id"]))
    for field, value in {"beforeStart": BEFORE, "beforeEnd": BEFORE, "afterStart": AFTER, "afterEnd": AFTER}.items():
        compare.locator(f"input[name={field}]").fill(value)
    compare.get_by_role("button", name="Сравнить периоды", exact=True).click()
    expect(page.locator("#memoryResult")).to_contain_text("изменение")
    page.locator("#memoryResult").screenshot(path=run / "history.png")
    comparison = post(browser, site, "memory/compare", {"mode": "synthetic", "series_id": selected["id"],
                      "before": [BEFORE, BEFORE], "after": [AFTER, AFTER]})
    assert comparison["delta"] == 5 and comparison["delta_percent"] == 25
    print("Browser, follow-up, approval, Redmine, simulation and PIM passed", flush=True)
    turns = post(browser, site, "qwen/history", {"dialog_id": dialog})["turns"]
    position = len(turns) + 1
    with page.expect_response(lambda response: response.url.endswith("/api/qwen/start")) as live_check:
        compare.get_by_role("button", name="Спросить в чате", exact=True).click()
        page.locator("#qwen-ask button").click()
    assert live_check.value.ok
    page.locator(f"#qwen-turn-{position}").wait_for(timeout=265_000)
    live_history = post(browser, site, "qwen/turn", {"dialog_id": dialog, "position": position})
    live_history_tools = [row["tool"] for row in live_history["trace"]
                          if row["step"] == "tool" and row["actor"] == "qwen"]
    assert "compare_history" in live_history_tools, live_history_tools
    write_json(run / "checks.json", {
        "report_id": report["run_id"], "investigation_seconds": report["seconds"],
        "model_tools": [row["tool"] for row in report["trace"] if row["step"] == "tool" and row["actor"] == "qwen"],
        "hypotheses": len(report["hypotheses"]), "source_statuses": report["sources_status"],
        "notification": notice["email_status"], "smtp_messages": notice["email_attempts"], "task_id": task["id"],
        "dialog_id": dialog, "tag": task["tag"], "target": task["effect_plan"]["target"],
        "redmine_action": export.get("action", "new_issue"), "simulation": effect["status"],
        "simulated_index": effect["measurement"]["kaspersky"], "history_delta": comparison["delta"],
        "history_live_tools": live_history_tools, "history_live_seconds": live_history["seconds"],
        "follow_up_saved_plan": True,
    })
    print("Separate live history request passed", flush=True)


def finish_rehearsal(run, site):
    from playwright.sync_api import expect, sync_playwright

    with sync_playwright() as play:
        browser = play.chromium.launch_persistent_context(run / "chrome-profile", channel="chrome",
                                                          headless=True, viewport={"width": 1400, "height": 950})
        try:
            restore_browser_session(browser, run)
            page = browser.pages[0]
            page.goto(site)
            dialogs = post(browser, site, "qwen/dialogs", {})["dialogs"]
            page.locator("#qwen-fab").click()
            if not dialogs:
                ask(page, QUESTION, 1)
                keep_browser_session(browser, run, site)
                page.locator("#qwen-mode").select_option("follow_up")
                ask(page, FOLLOW_UP, 2)
            expect(page.locator("#qwen-turn-2")).to_contain_text(FOLLOW_UP)
            dialog = page.locator("#qwen-dialogs").input_value()
            page.locator("#qwen-close").click()
            if not (run / "backlog-effect.png").is_file():
                page.locator("#journal summary").click()
                page.locator("#reloadRuns").click()
                show_simulation(run, page)
            complete_history(run, site, browser, page, dialog)
        finally:
            browser.close()


def verify_replay(run, site):
    from playwright.sync_api import expect, sync_playwright
    evidence = json.loads((run / "checks.json").read_text(encoding="utf8"))
    old_report = json.loads((run / "investigation.json").read_text(encoding="utf8"))
    with sync_playwright() as play:
        browser = play.chromium.launch_persistent_context(run / "chrome-profile", channel="chrome", headless=True)
        restore_browser_session(browser, run)
        page = browser.pages[0]
        requests = []
        page.on("request", lambda request: requests.append(request.url))
        page.goto(site + "#report=" + evidence["report_id"])
        expect(page.locator("#runDetails")).to_contain_text("Гипотезы Qwen")
        report = post(browser, site, "analysis/report", {"run_id": evidence["report_id"]})
        assert report == old_report and not any(url.endswith("/analysis/start") for url in requests)
        page.locator("#qwen-fab").click()
        expect(page.locator("#qwen-turn-2")).to_contain_text(FOLLOW_UP)
        assert post(browser, site, "tasks", {})["tasks"][0]["id"] == evidence["task_id"]
        awaiting = post(browser, site, "tasks/review", {"task_id": evidence["task_id"]})
        assert awaiting["status"] == "waiting_for_data"
        assert not os.getenv("PM_LLM_URL")
        browser.close()
    evidence["reopen_without_model"] = True
    write_json(run / "checks.json", evidence)
    print("Persisted report, dialogue and task reopened with Qwen URL absent", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("fresh", "replay", "rehearse", "finish", "verify-replay"))
    parser.add_argument("name")
    parser.add_argument("--live-replay", action="store_true")
    parser.add_argument("--port", type=int, default=18127)
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z0-9-]{1,40}", args.name):
        parser.error("Используйте строчные ASCII-буквы, цифры и дефисы.")
    fresh = args.mode in {"fresh", "rehearse"}
    run = BASE / "runs" / args.name
    if fresh:
        run.mkdir(parents=True)
    elif not (run / "db" / "ai.sqlite3").is_file():
        parser.error("Нет подготовленной базы демонстрации.")
    configure(run, live=fresh or args.mode == "finish" or (args.mode == "replay" and args.live_replay))
    chat_api.install()
    with ExitStack() as stack:
        server = stack.enter_context(ThreadingHTTPServer(("127.0.0.1", args.port), app.Handler))
        site = f"http://127.0.0.1:{server.server_port}/"
        stack.enter_context(background(server))
        if fresh:
            memory.write_observations([point(date, value) | {"product": PRODUCT} for date, value in ((BEFORE, 20), (AFTER, 25))])
            smtp = relay(stack, run, site)
            stop, worker = monitor.start(monitor.interval())
            stack.callback(stop.set)
            print(f"{site} · первый плановый тик через {monitor.interval()} с", flush=True)
        else:
            print(site, flush=True)
        if args.mode == "rehearse":
            exercise(run, site, smtp)
        elif args.mode == "finish":
            finish_rehearsal(run, site)
        elif args.mode == "verify-replay":
            verify_replay(run, site)
        else:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as play:
                browser = play.chromium.launch_persistent_context(run / "chrome-profile", channel="chrome", headless=False)
                restore_browser_session(browser, run)
                page = browser.pages[0]
                if not fresh and (run / "checks.json").exists():
                    site += "#report=" + json.loads((run / "checks.json").read_text(encoding="utf8"))["report_id"]
                page.goto(site)
                print("Ctrl+C или закрытие окна Chrome завершает демонстрацию.", flush=True)
                browser.wait_for_event("close", timeout=0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
