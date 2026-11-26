"""Read local competitor research as records accepted by PM_EVIDENCE."""

import hashlib
import io
import math
import re
from datetime import UTC, date, datetime, time
from os import PathLike
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

from .yandex_search.parsing import plain_text, safe_xml, utc_stamp

_MAX_FILE = 20 * 1024 * 1024
_MAX_MEMBER = 16 * 1024 * 1024
_MAX_EXPANDED = 64 * 1024 * 1024
_MAX_ROWS = 10_000
_MAX_COLUMNS = 80
_FORBIDDEN_SHEET = "эталонныйкейс"
_FIELDS = {
    "товар": "product", "продукт": "product", "product": "product",
    "конкурент": "competitor", "competitor": "competitor",
    "версия": "version", "version": "version",
    "тип лицензии": "license_type", "license type": "license_type",
    "срок": "duration", "срок лицензии": "duration", "duration": "duration",
    "устройства": "devices", "количество устройств": "devices", "devices": "devices",
    "цена": "price", "price": "price",
    "валюта": "currency", "currency": "currency",
    "ндс": "tax", "vat": "tax",
    "метрика": "metric", "показатель": "metric", "metric": "metric",
    "единица": "unit", "единица измерения": "unit", "unit": "unit",
    "дата исследования": "study_date", "дата проверки": "study_date", "study date": "study_date",
    "наличие": "availability", "availability": "availability",
    "подбор лицензии": "license_selection", "путь покупки": "purchase_flow",
    "консультация": "consultation",
}


def _archive_bytes(path):
    with path.open("rb") as stream:
        data = stream.read(_MAX_FILE + 1)
    if len(data) > _MAX_FILE:
        raise ValueError("research_file_too_large")
    return io.BytesIO(data)


def _inspect_zip(archive):
    entries = archive.infolist()
    if len(entries) > 4096 or sum(info.file_size for info in entries) > _MAX_EXPANDED:
        raise ValueError("research_archive_too_large")
    names = set()
    for info in entries:
        name = info.filename
        parts = PurePosixPath(name).parts
        if (
            name in names or not parts or name.startswith("/") or "\\" in name
            or ":" in name or ".." in parts or info.flag_bits & 1
            or info.file_size > _MAX_MEMBER
        ):
            raise ValueError("unsafe_research_archive")
        names.add(name)
        if name.lower().endswith((".xml", ".rels")):
            text = archive.read(info).decode("utf-8-sig")
            if "\x00" in text or re.search(r"<!\s*(DOCTYPE|ENTITY)\b", text, re.I):
                raise ValueError("unsafe_research_xml")


def _record(path, locator, text, metadata, **provenance):
    key = hashlib.sha256(f"{path.name}\0{locator}\0{text}".encode()).hexdigest()[:24]
    return {
        "id": "competitor:" + key,
        "ref": f"{path.name} · {locator}",
        "kind": "Конкуренты",
        "text": text[:2000],
        "provenance": {"file": path.name, **provenance},
        **metadata,
    }


def _study_date(value):
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    for pattern in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(value, pattern).date().isoformat()
        except (TypeError, ValueError):
            continue
    return None


def _display(value):
    if value is None or isinstance(value, float) and not math.isfinite(value):
        return ""
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    return plain_text(str(value), 500)


def _add_metadata(metadata, label, value):
    field = _FIELDS.get(label.casefold())
    if field == "study_date":
        parsed = _study_date(value)
        if parsed:
            metadata[field] = parsed
    elif field:
        text = _display(value)
        if text:
            metadata[field] = text


def _xlsx_records(path, data, limit):
    from openpyxl import load_workbook
    from openpyxl.utils import get_column_letter

    data.seek(0)
    book = load_workbook(data, read_only=True, data_only=True, keep_links=False)
    records = []
    try:
        for sheet in book.worksheets:
            if re.sub(r"[\W_]+", "", sheet.title.casefold()) == _FORBIDDEN_SHEET:
                continue
            headers = {}
            sheet.reset_dimensions()
            for index, row in enumerate(sheet.iter_rows(max_col=_MAX_COLUMNS, values_only=True), 1):
                if index > _MAX_ROWS:
                    break
                populated = [(col, value, _display(value)) for col, value in enumerate(row, 1)
                             if value is not None]
                cells = [(col, value, text) for col, value, text in populated if text]
                if not cells:
                    continue
                recognized = {col: text for col, _, text in cells if text.casefold() in _FIELDS}
                if len(recognized) >= 2 or len(recognized) == len(cells):
                    headers = recognized
                    continue
                metadata = {}
                for col, value, _ in cells:
                    _add_metadata(metadata, headers.get(col, ""), value)
                first = get_column_letter(cells[0][0])
                last = get_column_letter(cells[-1][0])
                area = f"{first}{index}" if first == last else f"{first}{index}:{last}{index}"
                text = " | ".join(
                    f"{headers.get(col, get_column_letter(col))}: {value}"
                    for col, _, value in cells
                )
                records.append(_record(path, f"{sheet.title}!{area}", text, metadata,
                                       sheet=sheet.title, range=area))
                if len(records) >= limit:
                    return records
    finally:
        book.close()
    return records


def _pptx_order(archive):
    relation = safe_xml(archive.read("ppt/_rels/presentation.xml.rels"), _MAX_MEMBER)
    presentation = safe_xml(archive.read("ppt/presentation.xml"), _MAX_MEMBER)
    targets = {}
    for node in relation.findall("{*}Relationship"):
        target = node.get("Target", "")
        if (
            node.get("TargetMode") != "External" and node.get("Type", "").endswith("/slide")
            and re.fullmatch(r"(?:/ppt/)?slides/slide[0-9]+\.xml", target)
        ):
            targets[node.get("Id")] = target.lstrip("/") if target.startswith("/") else "ppt/" + target
    for node in presentation.findall("{*}sldIdLst/{*}sldId"):
        rel_id = next((value for name, value in node.attrib.items() if name.endswith("}id")), None)
        if rel_id not in targets:
            raise ValueError("invalid_slide_reference")
        yield targets[rel_id]


def _pptx_records(path, archive, limit):
    records = []
    for position, entry in enumerate(_pptx_order(archive), 1):
        slide = safe_xml(archive.read(entry), _MAX_MEMBER)
        paragraphs = [
            plain_text("".join(node.itertext()), 500)
            for node in slide.iterfind(".//{*}p")
        ]
        text = "\n".join(line for line in paragraphs if line)
        if not text:
            continue
        metadata = {}
        for paragraph in paragraphs:
            name, separator, value = paragraph.partition(":")
            if separator:
                _add_metadata(metadata, name.strip(), value.strip())
        records.append(_record(path, f"слайд {position}", text, metadata, slide=position))
        if len(records) >= limit:
            break
    return records


def load_competitor_evidence(
    path: str | PathLike[str], *, limit: int = 200,
) -> list[dict]:
    """Read bounded XLSX/PPTX evidence locally. Invalid files raise ValueError or OSError."""
    if type(limit) is not int or not 1 <= limit <= 2000:
        raise ValueError("invalid_import_limit")
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix not in {".xlsx", ".pptx"}:
        raise ValueError("unsupported_research_format")
    data = _archive_bytes(path)
    try:
        with ZipFile(data) as archive:
            _inspect_zip(archive)
            entries = _xlsx_records(path, data, limit) if suffix == ".xlsx" else (
                _pptx_records(path, archive, limit)
            )
        return [item | {"imported_at": utc_stamp(datetime.now(UTC))} for item in entries]
    except (BadZipFile, ElementTree.ParseError, KeyError, TypeError, RuntimeError, UnicodeError) as exc:
        raise ValueError("invalid_research_document") from exc
