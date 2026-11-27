"""Check an isolated Qwen preview with the installed Chromium browser."""

import argparse
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright


def smoke(url, question, live, follow_up, effect_plan, measured_task):
    parsed = urlsplit(url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"} or parsed.username or parsed.password:
        raise ValueError("Use the isolated loopback preview")
    base = url.rstrip("/")
    with sync_playwright() as play:
        browser = play.chromium.launch(channel="chrome", headless=True)
        context = browser.new_context(viewport={"width": 1200, "height": 860})
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(type(error).__name__))
        page.goto(base)
        if live:
            expect(page.locator("#qwen-release")).to_be_visible()
        fab = page.locator("#qwen-fab")
        expect(fab).to_be_visible()
        anchor = fab.bounding_box()
        page.mouse.move(anchor["x"] + 20, anchor["y"] + 20)
        page.mouse.down()
        page.mouse.move(anchor["x"] - 75, anchor["y"] - 60, steps=4)
        page.mouse.up()
        assert fab.bounding_box()["x"] < anchor["x"] - 30
        fab.click()
        panel = page.locator("#qwen-panel")
        expect(panel).to_be_visible()
        prompt = page.locator("#qwen-question")
        ask = panel.get_by_role("button", name="Спросить", exact=True)
        expect(ask).to_be_enabled()
        prompt.fill(question)
        prompt.press("Shift+Enter")
        assert prompt.input_value().endswith("\n")
        with page.expect_response(lambda r: r.url.endswith("/api/qwen/start"), timeout=10000) as started:
            prompt.press("Enter")
        assert started.value.ok
        expect(panel.locator("#qwen-log article").last).to_contain_text(question, timeout=180000)
        dialog_id = page.locator("#qwen-dialogs").input_value()
        turns = context.request.post(base + "/api/qwen/history", data={"dialog_id": dialog_id}).json()["turns"]
        reply = context.request.post(base + "/api/qwen/turn", data={
            "dialog_id": dialog_id, "position": turns[-1]["position"],
        }).json()
        assert reply["sources"]
        assert reply["trace"]
        if live:
            assert reply["llm_status"] == "ok", reply["llm_status"]
            assert any(step.get("actor") == "qwen" and step.get("tool") for step in reply["trace"])
        steps = panel.get_by_text("Шаги расследования", exact=True)
        steps.click()
        expect(steps).to_be_visible()
        drafts = panel.get_by_text("Черновики задач", exact=True)
        drafts.click()
        draft = panel.locator("form").filter(has=page.get_by_role("button", name="Подтвердить сохранение в бэклоге")).first
        title = "Проверка редактирования черновика в изолированной БД"
        problem = "Проверить снижение завершённых заказов по журналу."
        draft.get_by_label("Название").fill(title)
        draft.get_by_label("Описание проблемы").fill(problem)
        draft.get_by_label("Цель четырёхнедельного индекса").fill("100")
        with page.expect_response(lambda r: r.url.endswith("/api/qwen/approve")) as approval:
            draft.get_by_role("button", name="Подтвердить сохранение в бэклоге").click()
        saved = approval.value.json()
        assert approval.value.ok, list(saved)
        repeated = context.request.post(base + "/api/qwen/approve", data=approval.value.request.post_data_json)
        assert repeated.ok and repeated.json()["id"] == saved["id"]
        assert saved["tag"] == "сгенерирована ИИ-помощником"
        assert saved["title"] == title
        assert saved["problem_description"] == problem
        if effect_plan:
            expect(ask).to_be_enabled()
            prompt.fill("Как оценить эффект?")
            prompt.press("Enter")
            planned = panel.locator("#qwen-log article").last
            expect(planned.get_by_text("Как измерить эффект", exact=True)).to_be_visible(timeout=180000)
            expect(planned.get_by_text("Базовый период ·", exact=False)).to_be_visible()
            expect(planned.get_by_text("Цель · требует согласования", exact=True)).to_be_visible()
            expect(planned.get_by_role("link", name="Предыдущий вопрос")).to_have_attribute("href", "#qwen-turn-1")
        if follow_up:
            question_two = "Какие доказательства подтверждают эту гипотезу?"
            expect(ask).to_be_enabled()
            prompt.fill(question_two)
            with page.expect_response(lambda r: r.url.endswith("/api/qwen/start"), timeout=10000) as started:
                prompt.press("Enter")
            assert started.value.ok
            second = panel.locator("#qwen-log article").last
            expect(second).to_contain_text(question_two, timeout=180000)
            previous_position = 2 if effect_plan else 1
            expect(second.get_by_role("link", name="Предыдущий вопрос")).to_have_attribute("href", f"#qwen-turn-{previous_position}")
            expect(second.get_by_text("Источники из предыдущего ответа")).to_be_visible()
        observed = context.request.post(base + "/api/tasks/review", data={"task_id": saved["id"]}).json()
        assert observed["status"] == "waiting_for_data"
        page.reload()
        expect(fab).to_be_visible()
        assert fab.bounding_box()["x"] < anchor["x"] - 30
        fab.click()
        expect(panel.get_by_text(question, exact=True)).to_be_visible()
        if effect_plan or measured_task:
            page.locator("#journal summary").click()
            page.locator("#reloadRuns").click()
            for label, status in [(title if effect_plan else None, "Ожидаются четыре полные недели"),
                                  (measured_task, "Цель не достигнута")]:
                if not label:
                    continue
                expect(ask).to_be_enabled()
                page.keyboard.press("Escape")
                row = page.locator("#serverTasks article").filter(has_text=label)
                row.get_by_role("button", name="Обсудить эффект в чате").click()
                ask.click()
                latest = panel.locator("#qwen-log article").last
                expect(latest.get_by_text(status, exact=False).first).to_be_visible(timeout=30000)
                if label == measured_task:
                    expect(latest.get_by_text("Следующий шаг · Пересмотреть гипотезу", exact=True)).to_be_visible()
                    latest.get_by_text("Расчёт эффекта", exact=True).click()
                    expect(latest.get_by_text("Разница изменений с контрольным рядом", exact=False)).to_be_visible()
        dialog_id = page.locator("#qwen-dialogs").input_value()
        other = browser.new_context()
        assert not other.request.post(base + "/api/qwen/history", data={"dialog_id": dialog_id}).ok
        assert context.request.post(base + "/api/tasks", data={}).json()["tasks"][0]["id"] == saved["id"]
        page.set_viewport_size({"width": 375, "height": 710})
        box = panel.bounding_box()
        assert box["x"] >= 0 and box["x"] + box["width"] <= 375
        page.keyboard.press("Escape")
        expect(panel).to_be_hidden()
        assert not errors, errors
        print("Browser: drag, Enter, details, edit, confirm, replay, private history, mobile, shared backlog, effect waiting"
              + (", previous question and citations" if follow_up else "")
              + (", saved effect plan" if effect_plan else "") + (", measured effect" if measured_task else ""))
        browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--isolated-database", action="store_true", required=True,
                        help="Confirm the preview uses a disposable state directory")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--follow-up", action="store_true")
    parser.add_argument("--effect-plan", action="store_true")
    parser.add_argument("--measured-task", help="Title of a seeded task with a failed measurement and a control row")
    parser.add_argument("--question", default="Почему снизились заказы? Проверь воронку, интервью, конкурентов и бэклог.")
    args = parser.parse_args()
    smoke(args.url, args.question, args.live, args.follow_up, args.effect_plan, args.measured_task)
