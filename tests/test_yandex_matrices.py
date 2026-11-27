import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from zipfile import ZipFile

from openpyxl import Workbook

from pm.competitor_sources import load_competitor_evidence, merge_competitor_corpus
from scripts.search_import_competitors import ROOT


class MatrixImportTests(unittest.TestCase):
    def setUp(self):
        self.private = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_nested_excel_headers_and_only_real_vertical_merge_inheritance(self):
        path = self.private / "synth.xlsx"
        book = Workbook()
        ws = book.active
        ws.title = "Сравнение"
        ws.append([None, "Конкурент: Демо A", None, "Конкурент: Демо B"])
        ws.merge_cells("B1:C1")
        ws.merge_cells("D1:E1")
        ws.append(["Продукт", "Цена", "Валюта", "Цена", "Валюта"])
        ws.append(["Демо Pro", 100, "RUB", 200, "RUB"])
        ws.append([None, 90, "RUB", 190, "RUB"])
        ws.merge_cells("A3:A4")
        ws.append([None, 99, "RUB", "=50+70", "RUB"])
        other = book.create_sheet("Резервная таблица")
        other.append(["Конкурент", "Поддержка"])
        other.append(["Третий", "Да"])
        book.create_sheet("Эталонный кейс").append(["ИСКЛЮЧИТЬ"])
        book.save(path)
        book.close()

        records = load_competitor_evidence(path)
        self.assertEqual(len(records), 7)
        self.assertEqual([(r.get("competitor"), r.get("price")) for r in records[:4]],
                         [("Демо A", "100"), ("Демо B", "200"), ("Демо A", "90"), ("Демо B", "190")])
        self.assertEqual(records[2]["product"], "Демо Pro")
        self.assertEqual(records[2]["provenance"]["merged_cells"], ["Сравнение!A3"])
        self.assertNotIn("product", records[4])
        self.assertNotIn("price", records[5])
        self.assertEqual(records[-1]["support"], "Да")
        self.assertNotIn("ИСКЛЮЧИТЬ", json.dumps(records, ensure_ascii=False))
        self.assertEqual(len({r["id"] for r in records}), len(records))

    def test_transposed_competitor_matrix_uses_explicit_group(self):
        path = self.private / "synth.xlsx"
        book = Workbook()
        ws = book.active
        ws.append([None, "Конкуренты"])
        ws.merge_cells("B1:C1")
        ws.append(["Характеристика", "Демо A", "Демо B"])
        ws.append(["Поддержка", "Да", "Нет"])
        ws.append(["Продукт", "Демо Pro", "Демо Pro"])
        ws.append(["Дата исследования", "27.11.2026", "2026-11-27"])
        book.save(path)
        book.close()
        records = load_competitor_evidence(path)
        self.assertEqual(len(records), 6)
        self.assertEqual(records[1]["support"], "Нет")
        self.assertEqual(records[-1]["study_date"], "2026-11-27")
        self.assertEqual(records[0]["offer_group"], records[4]["offer_group"])
        self.assertNotEqual(records[0]["offer_group"], records[1]["offer_group"])

    def test_presentation_table_with_slide_date_and_ordered_provenance(self):
        path = self.private / "synth.pptx"
        def cell(text):
            return f"<a:tc><a:txBody><a:p><a:r><a:t>{text}</a:t></a:r></a:p></a:txBody></a:tc>"
        grid = [
            ("Характеристика", "Конкурент: Демо A", "Конкурент: Демо B"),
            ("Способы оплаты", "Карты", "Счёт"),
        ]
        rows = "".join("<a:tr>" + "".join(map(cell, row)) + "</a:tr>" for row in grid)
        with ZipFile(path, "w") as archive:
            archive.writestr("ppt/presentation.xml", '<p xmlns:r="urn:r"><sldIdLst><sldId r:id="r1"/></sldIdLst></p>')
            archive.writestr("ppt/_rels/presentation.xml.rels",
                             '<Relationships><Relationship Id="r1" Type="a/slide" Target="slides/slide1.xml"/></Relationships>')
            archive.writestr("ppt/slides/slide1.xml", f"""
            <slide xmlns:a="urn:a"><a:p><a:r><a:t>Дата исследования: 27.11.2026</a:t></a:r></a:p>
            <a:tbl>{rows}</a:tbl></slide>""")
        head, a, b = load_competitor_evidence(path)
        self.assertEqual(head["study_date"], "2026-11-27")
        self.assertNotIn("Карты", head["text"])
        self.assertEqual(a["payment"], "Карты")
        self.assertEqual(b["payment"], "Счёт")
        self.assertEqual(b["study_date"], "2026-11-27")
        self.assertEqual(b["provenance"], {"file": "synth.pptx", "slide": 1, "table": 1,
                                             "row": 2, "columns": [1, 3]})

    def test_cli_can_extend_corpus_without_replacing_interviews(self):
        book_path = self.private / "synth.xlsx"
        workbook = Workbook()
        workbook.active.append(["Конкурент", "Поддержка"])
        workbook.active.append(["Демо A", "Да"])
        workbook.save(book_path)
        workbook.close()
        existing = self.private / "original.json"
        merged = self.private / "merged.json"
        interview = {"id": "interview", "ref": "Демо", "text": "Синтетика", "kind": "Интервью"}
        original = [interview, *load_competitor_evidence(book_path)]
        existing.write_text(json.dumps(original), encoding="utf-8")
        run = subprocess.run([sys.executable, str(ROOT / "scripts/search_import_competitors.py"),
                              str(book_path), "--corpus", str(existing), "--output", str(merged)],
                             capture_output=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(merged.read_text(encoding="utf-8")), original)
        self.assertEqual(json.loads(existing.read_text(encoding="utf-8")), original)
        with self.assertRaises(ValueError):
            merge_competitor_corpus([{"id": "broken"}], [])
