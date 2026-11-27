import os
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import patch

from pm import live_runs, qwen


@patch.dict(os.environ, {"PM_QWEN_RUNTIME_PROFILE": "sandy-bridge-one-slot"})
class SandyBridgeTests(unittest.TestCase):
    def test_one_active_one_waiting_and_third_rejected(self):
        entered, queued, release = Event(), Event(), Event()
        order = []

        @qwen.single_slot_investigation
        def analyze(label, *, progress=None, _budget=None):
            self.assertEqual(_budget, qwen.SANDY_BRIDGE_BUDGET)
            order.append(label)
            if label == "first":
                entered.set()
                self.assertTrue(release.wait(4))
            return label

        def progress(event):
            self.assertEqual(event, {"step": "queue", "status": "waiting"})
            queued.set()

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(analyze, "first")
            try:
                self.assertTrue(entered.wait(2))
                second = pool.submit(analyze, "second", progress=progress)
                self.assertTrue(queued.wait(2))
                with self.assertRaises(qwen.InvestigationQueueError):
                    analyze("third")
                self.assertEqual(order, ["first"])
            finally:
                release.set()
            self.assertEqual(first.result(), "first")
            self.assertEqual(second.result(), "second")
        self.assertEqual(order, ["first", "second"])

    def test_job_reports_queue_error_without_exposing_private_job(self):
        def rejected(_request, *, progress):
            raise qwen.InvestigationQueueError("Queue wait expired")

        job = live_runs.start({}, rejected, private=True)
        self.assertGreater(job["wait_seconds"], 2 * qwen.SANDY_BRIDGE_BUDGET.total)
        with self.assertRaises(ValueError):
            live_runs.status(job["job_id"])
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            state = live_runs.status(job["job_id"], private=True)
            if state["status"] != "running":
                break
            time.sleep(0.01)
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["message"], "Queue wait expired")