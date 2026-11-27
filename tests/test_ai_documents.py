import concurrent.futures
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

from openpyxl import Workbook
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from pm import ai_store, documents, research
from pm.settings import ROOT


class DocumentTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ, {"PM_PRIVATE_DIR": str(self.folder), "PYTHONIOENCODING": "utf8"}))

    def test_office_and_pdf_extract_with_location(self):
        doc = self.folder / "interview.docx"
        slides = self.folder / "offer.pptx"
        content = "<document><p><t>Условия доставки</t></p></document>"
        for path, member in ((doc, "word/document.xml"), (slides, "ppt/slides/slide4.xml")):
            with ZipFile(path, "w") as archive:
                archive.writestr(member, content)
                archive.writestr("unrelated.xml", "<t>ignore this</t>")
        self.assertEqual(documents.snippets(doc)[0], ({"paragraph": 1, "part": 1}, "Условия доставки"))
        self.assertEqual(documents.snippets(slides)[0][0]["slide"], 4)

        pdf = self.folder / "note.pdf"
        writer = PdfWriter()
        page = writer.add_blank_page(width=200, height=100)
        font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                                 NameObject("/Subtype"): NameObject("/Type1"),
                                 NameObject("/BaseFont"): NameObject("/Helvetica")})
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
        stream = DecodedStreamObject()
        stream.set_data(b"BT /F1 12 Tf 10 20 Td (Checkout conditions) Tj ET")
        page[NameObject("/Contents")] = writer._add_object(stream)
        writer.write(pdf)
        self.assertEqual(documents.snippets(pdf)[0], ({"page": 1, "part": 1}, "Checkout conditions"))

    def test_preview_does_not_write_and_import_is_reproducible(self):
        path = self.folder / "sample.txt"
        path.write_text("Процесс заказа лицензии", encoding="utf8")
        base = [sys.executable, "-B", str(ROOT / "app.py"), "import", "--file", str(path), "--kind", "Интервью"]
        preview = subprocess.run(base + ["--preview"], capture_output=True, text=True,
                                 encoding="utf8", check=True)
        self.assertEqual(json.loads(preview.stdout)["total"], 1)
        self.assertFalse((self.folder / "ai.sqlite3").exists())
        for _ in range(2):
            subprocess.run(base, capture_output=True, text=True, encoding="utf8", check=True)
        self.assertEqual(len(research.imported_sources()), 1)

    def test_reference_sheet_and_attached_json_are_excluded(self):
        path = self.folder / "case.xlsx"
        book = Workbook()
        book.active.title = "Эталонный кейс"
        book.active["B2"] = "Synthetic excluded answer"
        book.save(path)
        with self.assertRaises(ValueError):
            research.range_excerpts(path, "Эталонный кейс", "A1:C3", "Бэклог")
        self.assertEqual(research.visible_excerpts([{"ref": "Case", "locator": {"sheet": "Эталонный_кейс"}}]), [])

    def test_sqlite_source_imports_do_not_drop_concurrent_writes(self):
        def store(i):
            return ai_store.store_excerpts(f"doc:{i}", [dict(id=str(i), text="Synthetic")])
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as workers:
            self.assertEqual(list(workers.map(store, range(18))), [1] * 18)
        self.assertEqual(len(ai_store.stored_excerpts()), 18)
