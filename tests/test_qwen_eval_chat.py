"""Regression coverage for the chat layer and its private integration contract."""
import os
import tempfile
import threading
import unittest
from http.client import HTTPMessage
from secrets import token_urlsafe
from unittest.mock import Mock, patch

from llm_lab.integration import chat_api, chat_store
import app
from pm import ai_store, brief, chat, investigation


class ChatTests(unittest.TestCase):
    def setUp(self):
        folder = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.dict("os.environ", {"PM_PRIVATE_DIR": folder}, clear=True))
        chat_store.install()

    def test_proxy_mode_requires_authenticated_principal_and_stable_ownership(self):
        key = token_urlsafe(32)

        def request(user=None, proof=None):
            headers = HTTPMessage()
            if user:
                headers[chat_api.PROXY_USER_HEADER] = user
            if proof:
                headers[chat_api.PROXY_KEY_HEADER] = proof
            return Mock(headers=headers)

        with patch.dict(os.environ, {"PM_QWEN_OWNER_MODE": "proxy", "PM_QWEN_PROXY_KEY": ""}):
            with self.assertRaisesRegex(ValueError, "PM_QWEN_PROXY_KEY"):
                chat_api.install()
        with patch.dict(os.environ, {"PM_QWEN_OWNER_MODE": "proxy", "PM_QWEN_PROXY_KEY": key}):
            for handler in (request(), request("reviewer"), request("reviewer", token_urlsafe(32))):
                with self.assertRaises(PermissionError):
                    chat_api.request_owner(handler, create=True)

            owner, cookie = chat_api.request_owner(request("reviewer", key))
            self.assertIsNone(cookie)
            self.assertEqual(chat_api.request_owner(request("reviewer", key), create=True), (owner, None))
            other, _ = chat_api.request_owner(request("second-reviewer", key))
            dialog = chat_store.create(owner)
            self.assertEqual(chat_api.route("dialogs", owner, {})["dialogs"][0]["id"], dialog)
            self.assertEqual(chat_api.route("dialogs", other, {}), {"dialogs": []})
            duplicated = request("reviewer", key)
            duplicated.headers[chat_api.PROXY_USER_HEADER] = "reviewer"
            with self.assertRaises(PermissionError):
                chat_api.request_owner(duplicated)

    def test_chat_progress_cannot_be_read_through_the_shared_api(self):
        import time

        dialog = chat_store.create("owner-a")
        body = {"dialog_id": dialog, "question": "Почему упали заказы?"}
        job = chat_api.route("start", "owner-a", body)["job_id"]
        with self.assertRaisesRegex(ValueError, "недоступен"):
            app.api("/api/analysis/status", {"job_id": job})
        with self.assertRaisesRegex(ValueError, "недоступен"):
            chat_api.route("status", "owner-b", body | {"job_id": job})
        for _ in range(100):
            state = chat_api.route("status", "owner-a", body | {"job_id": job})
            if state["status"] != "running":
                break
            time.sleep(0.02)
        self.assertEqual(state["status"], "ready")
        self.assertEqual(chat_store.history("owner-a", dialog)[0]["question"], body["question"])

    def test_private_follow_up_and_new_scope(self):
        analyzer = Mock(wraps=investigation.analyze)
        first = chat.consult({"question": "Почему заказы снизились?"}, analyze=analyzer)
        result = chat.consult({"question": "А что ей противоречит?"}, previous=first, analyze=analyzer)
        args, kwargs = analyzer.call_args
        self.assertIn("Предыдущий вопрос: Почему заказы снизились?", args[0]["query"])
        self.assertFalse(kwargs["persist"])
        self.assertEqual(result["reply"]["previous_run_id"], first["report"]["run_id"])
        chat.consult({"question": "Какие есть задачи в бэклоге?", "mode": "new_topic"},
                     previous=result, analyze=analyzer)
        self.assertNotIn("Предыдущий вопрос", analyzer.call_args.args[0]["query"])
        self.assertFalse(ai_store.recent())

    def test_follow_up_distinguishes_prior_citations_from_fresh_evidence(self):
        previous = chat.consult({"question": "Почему упали заказы?"})
        fresh = chat.consult({"question": "Какие доказательства подтверждают эту гипотезу?"},
                             previous=previous)
        cited = set(previous["report"]["hypotheses"][0]["evidence_ids"])
        self.assertEqual({row["id"] for row in fresh["reply"]["previous_evidence"]}, cited)
        self.assertNotEqual(fresh["report"]["run_id"], previous["report"]["run_id"])

    def test_chat_preserves_model_tool_trace_and_clarifies_missing_windows(self):
        steps = iter(["sales", "funnel", "finish"])
        def decide(*_):
            tool = next(steps)
            return dict(tool=tool, arguments={}, purpose="enough_evidence" if tool == "finish" else "measure"), {}

        def run(request, **kwargs):
            return investigation.analyze(request, decide=decide, **kwargs)

        turn = chat.consult({"question": "Почему упали заказы? Проверь воронку."}, analyze=run)
        refs = {source["id"] for source in turn["reply"]["sources"]}
        self.assertTrue({"orders:0", "reach:desktop"} <= refs)
        tools = [event["tool"] for event in turn["reply"]["trace"] if event.get("actor") == "qwen"
                 and event.get("tool")]
        self.assertEqual(tools, ["sales", "funnel"])
        unused = Mock()
        result = chat.consult({"question": "Что было год назад?"}, analyze=unused)
        self.assertEqual(result["reply"]["needs"], "historical_windows")
        unused.assert_not_called()

    def test_run_is_reused_by_brief(self):
        saved = investigation.analyze({"query": "Что изменилось после релиза?"})
        result = brief.release_brief(saved["run_id"])
        self.assertEqual(result["run_id"], saved["run_id"])
        self.assertEqual(result["sources"], ai_store.run(saved["run_id"])["evidence"])
        self.assertEqual(len(ai_store.recent()), 1)

    def test_effect_plan_uses_the_previous_report_when_its_scope_matches(self):
        previous = chat.consult({"question": "Почему снизились заказы?"})
        analyze = Mock(side_effect=AssertionError("The accepted plan already exists"))
        fresh = chat.consult({"question": "Как оценить эффект?"}, previous=previous, analyze=analyze)
        self.assertEqual(fresh["previous_run_id"], previous["report"]["run_id"])
        self.assertEqual(fresh["reply"]["effect_plan"], previous["report"]["effect_plan"])
        self.assertIsNone(fresh["reply"]["effect_plan"]["target"])
        self.assertTrue(fresh["reply"]["expand_effect_plan"])
        self.assertFalse(fresh["reply"]["usage"])
        self.assertFalse(fresh["reply"]["drafts"])
        analyze.assert_not_called()

    def test_owner_bound_dialog_and_concurrent_append(self):
        key = chat_store.create("owner-a")
        turn = {"question": "Private user question", "reply": {"answer": "Response"}}
        barrier = threading.Barrier(2)
        outcomes = []
        def compete():
            barrier.wait()
            try:
                chat_store.append("owner-a", key, 0, turn)
                outcomes.append(True)
            except ValueError:
                outcomes.append(False)
        workers = [threading.Thread(target=compete) for _ in range(2)]
        for thread in workers:
            thread.start()
        for thread in workers:
            thread.join()
        self.assertCountEqual(outcomes, [True, False])
        self.assertEqual(chat_store.history("owner-a", key)[0]["question"], turn["question"])
        self.assertEqual(chat_store.dialogs("owner-b"), [])
        for action in ("history", "turn", "approve"):
            with self.assertRaisesRegex(ValueError, "не найден"):
                chat_api.route(action, "owner-b", {"dialog_id": key, "position": 1, "confirm": True})

    def test_only_confirmed_existing_proposal_is_forwarded(self):
        turn = chat.consult({"question": "Почему упали заказы?"})
        report = turn["report"]
        proposal = report["proposed_tasks"][0]
        approve = Mock(return_value={"id": "approved"})
        fields = {"proposal_id": proposal["id"], "target": 120}
        with patch.object(ai_store, "decide", approve):
            with self.assertRaises(ValueError):
                chat_api.approve(turn, fields)
            with self.assertRaises(ValueError):
                chat_api.approve(turn, fields | {"confirm": True, "draft": {"evidence_ids": []}})
            approve.assert_not_called()
            chat_api.approve(turn, fields | {"confirm": True, "draft": {
                "title": "Новый заголовок", "problem_description": "Пропуски в журнале событий.",
                "verification": "Сопоставить корзину с журналом событий.",
            }})
        args, kwargs = approve.call_args
        self.assertEqual(args[0]["action"], "accept")
        self.assertEqual(kwargs["edited"]["title"], "Новый заголовок")
        self.assertEqual(kwargs["edited"]["problem_description"], "Пропуски в журнале событий.")
        self.assertEqual(kwargs["edited"]["criteria"][0], kwargs["edited"]["verification"])
        self.assertNotIn("request_context", kwargs["source_report"])
        self.assertEqual(kwargs["source_report"]["proposed_tasks"][0]["tag"], ai_store.TASK_TAG)
