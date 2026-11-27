import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

from openpyxl import Workbook

from pm import research
from pm.competitor_sources import load_competitor_evidence, merge_competitor_corpus
from scripts.search_import_competitors import ROOT, write_private_json
from scripts.search_smoke import offline_search


def synthetic_book(path):
    book = Workbook()
    sheet = book.active
    sheet.title = "Синтетика"
    sheet.append(["Продукт", "Конкурент", "Дата исследования", "Метрика", "Единица", "Цена"])
    sheet.append(["Демо Pro", "Демо-магазин", date(2026, 11, 3), "Стоимость лицензии", "руб.", 0])
    sheet.append(["Демо Pro", "Второй синтетический магазин", None, "Время оформления", "с"])
    sheet.append(["Синтетический комментарий"])
    sheet.merge_cells("A4:D4")
    sheet["A5"] = "=SUM(10,12)"
    for name in ("Эталонный_кейс", "ЭТАЛОННЫЙ-кейс"):
        book.create_sheet(name).append(["EXCLUDED SYNTHETIC VALUE"])
    book.save(path)
    book.close()


def synthetic_deck(path, *, first_title="Продукт: Демо Pro"):
    with ZipFile(path, "w") as archive:
        archive.writestr("ppt/presentation.xml", """<p:presentation xmlns:p="urn:p" xmlns:r="urn:r">
            <p:sldIdLst><p:sldId id="256" r:id="r2"/><p:sldId id="257" r:id="r1"/></p:sldIdLst>
            </p:presentation>""")
        archive.writestr("ppt/_rels/presentation.xml.rels", """<Relationships xmlns="urn:rels">
            <Relationship Id="r1" Type="urn:p/slide" Target="slides/slide1.xml"/>
            <Relationship Id="r2" Type="urn:p/slide" Target="/ppt/slides/slide2.xml"/>
            </Relationships>""")
        archive.writestr("ppt/slides/slide1.xml", """<p:sld xmlns:p="urn:p" xmlns:a="urn:a">
            <a:p><a:r><a:t>Второй по порядку</a:t></a:r></a:p></p:sld>""")
        archive.writestr("ppt/slides/slide2.xml", f"""<p:sld xmlns:p="urn:p" xmlns:a="urn:a">
            <a:p><a:r><a:t>{first_title}</a:t></a:r></a:p>
            <a:p><a:r><a:t>Дата проверки: 02.11.2026</a:t></a:r></a:p></p:sld>""")


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_xlsx_provenance_and_corpus_compatibility(self):
        workbook = self.directory / "synthetic.xlsx"
        output = self.directory / "competitors.json"
        synthetic_book(workbook)
        entries = load_competitor_evidence(workbook)
        self.assertEqual(len(entries), 3)
        self.assertEqual([x["id"] for x in entries], [x["id"] for x in load_competitor_evidence(workbook)])
        self.assertEqual(load_competitor_evidence(workbook, limit=1)[0]["id"], entries[0]["id"])
        priced, undated, merged = entries
        self.assertEqual(priced["provenance"], {
            "file": "synthetic.xlsx", "sheet": "Синтетика", "range": "A2:F2",
        })
        self.assertEqual(priced["product"], "Демо Pro")
        self.assertEqual(priced["study_date"], "2026-11-03")
        self.assertEqual(priced["price"], "0")
        self.assertEqual(priced["unit"], "руб.")
        self.assertNotIn("study_date", undated)
        self.assertEqual(merged["provenance"]["range"], "A4")
        self.assertNotIn("EXCLUDED", json.dumps(entries))
        write_private_json(output, entries)
        with patch.dict(os.environ, {"PM_EVIDENCE": str(output), "PM_PRIVATE_DIR": str(self.directory)}):
            result = research.search("стоимость лицензии", research.sources())
        self.assertEqual(result[0]["id"], priced["id"])
        self.assertEqual(result[0]["kind"], "Конкуренты")

    def test_pptx_uses_presentation_order_with_only_explicit_metadata(self):
        deck = self.directory / "synthetic.pptx"
        synthetic_deck(deck)
        front, back = load_competitor_evidence(deck)
        self.assertEqual(front["provenance"], {"file": "synthetic.pptx", "slide": 1})
        self.assertEqual(front["product"], "Демо Pro")
        self.assertEqual(front["study_date"], "2026-11-02")
        self.assertEqual(back["text"], "Второй по порядку")
        self.assertNotIn("study_date", back)

    def test_explicit_reference_slide_is_dropped_without_renumbering_provenance(self):
        deck = self.directory / "labeled.pptx"
        synthetic_deck(deck, first_title="Эталонный_ответ: пример")
        slide, = load_competitor_evidence(deck)
        self.assertEqual(slide["provenance"]["slide"], 2)
        answer = slide | {"id": "reference", "ref": "legacy.xlsx · Эталонный кейс!A1"}
        self.assertEqual(merge_competitor_corpus([answer, slide], [slide]), [slide])

    def test_malformed_and_unsafe_archives(self):
        file = self.directory / "malformed.pptx"
        file.write_bytes(b"bad zip")
        with self.assertRaises(ValueError):
            load_competitor_evidence(file)
        for name, body in (
            ("../escape.xml", "<root/>"),
            ("C:/escape.xml", "<root/>"),
            ("/escape.xml", "<root/>"),
            ("presentation.xml", "<!DOCTYPE a [<!ENTITY b 'test'>]><a/>"),
        ):
            with ZipFile(file, "w") as archive:
                archive.writestr(name, body)
            with self.subTest(name=name), self.assertRaises(ValueError):
                load_competitor_evidence(file)

    def test_refuse_output_inside_git_or_existing_and_run_cli(self):
        with self.assertRaises(ValueError):
            write_private_json(ROOT / "forbidden.json", [])
        outfile = self.directory / "out.json"
        infile = self.directory / "local.xlsx"
        synthetic_book(infile)
        command = [sys.executable, str(ROOT / "scripts/search_import_competitors.py"),
                   str(infile), "--output", str(outfile)]
        process = subprocess.run(command, capture_output=True)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(len(json.loads(outfile.read_text(encoding="utf-8"))), 3)
        retry = subprocess.run(command, capture_output=True)
        self.assertEqual(retry.returncode, 2)
        self.assertEqual(len(json.loads(outfile.read_text(encoding="utf-8"))), 3)

    def test_snapshot_is_a_separate_dated_capture(self):
        path = self.directory / "snapshot.json"
        command = [sys.executable, str(ROOT / "scripts/search_smoke.py"), "--snapshot", str(path)]
        completed = subprocess.run(command, capture_output=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(saved, json.loads(completed.stdout))
        self.assertEqual(saved["mode"], "offline")
        self.assertTrue(saved["items"][0]["retrieved_at"].endswith("Z"))

    def test_offline_smoke_works_with_blank_environment(self):
        with patch.dict(os.environ, {"YANDEX_SEARCH_ENABLED": "false"}):
            result = offline_search()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["usage"], {"requests": 1, "cache_hits": 0})
        self.assertEqual(result["items"][0]["ref"], "https://example.com/demo-pro")
