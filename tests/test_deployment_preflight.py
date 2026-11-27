"""Synthetic validation at the same entry points used by systemd."""
import importlib.util
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

MODULE = Path(__file__).resolve().parents[1] / "scripts/deploy/preflight.py"
spec = importlib.util.spec_from_file_location("deployment_preflight", MODULE)
preflight = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preflight)


class ReleaseConfiguration(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.documents = self.root / "docs"
        self.documents.mkdir()
        self.enterContext(patch.object(preflight, "DB", self.root))
        self.enterContext(patch.object(preflight, "DOCS", self.documents))
        self.enterContext(patch.dict(os.environ, {
            "PARADIIGMA_MODE": "demo", "PM_PRIVATE_DIR": str(self.root),
        }, clear=True))

    def test_demo_starts_with_persistent_schema_and_loopback_model(self):
        os.environ["PM_LLM_URL"] = "http://127.0.0.1:18081/v1"
        preflight.validate()
        self.assertTrue((self.root / "ai.sqlite3").is_file())

    def test_demo_rejects_tokens_files_and_untrusted_endpoint(self):
        for settings in ({"METRIKA_TOKEN": "fixture", "METRIKA_COUNTER_ID": "4"},
                         {"YANDEX_SEARCH_ENABLED": "true"}, {"PM_EVIDENCE": str(self.root)},
                         {"PM_LLM_URL": "https://external.invalid/"},
                         {"PM_PRIVATE_DIR": "."}):
            with self.subTest(keys=list(settings)), patch.dict(os.environ, settings), self.assertRaises(ValueError):
                preflight.validate()

    def test_private_inputs_require_approved_directory(self):
        allowed = self.documents / "input.json"
        allowed.write_text("[]", encoding="utf8")
        with patch.dict(os.environ, PARADIIGMA_MODE="private", PM_EVIDENCE=str(allowed)):
            preflight.validate()
            with patch.dict(os.environ, PM_EVIDENCE=str(MODULE)), self.assertRaises(ValueError):
                preflight.validate()
            allowed.write_text("invalid JSON", encoding="utf8")
            with self.assertRaises(ValueError):
                preflight.validate()
