"""Interpret explicitly labelled evidence grids from Excel and PowerPoint."""

import re
from collections import defaultdict
from itertools import islice
from xml.etree import ElementTree

from pm.competitor_sources import (
    _FIELDS, _MAX_COLUMNS, _MAX_MEMBER, _MAX_ROWS, _display, _read_metadata, _record,
)
from .parsing import plain_text, safe_xml

_BAND = re.compile(r"^(?:конкурент|competitor)\s*[:–—]\s*(.+)$", re.I)


def workbook_merges(archive):
    relations = safe_xml(archive.read("xl/_rels/workbook.xml.rels"), _MAX_MEMBER)
    book = safe_xml(archive.read("xl/workbook.xml"), _MAX_MEMBER)
    targets = {}
    for link in relations.findall("{*}Relationship"):
        target = link.get("Target", "").removeprefix("/xl/")
        if (
            link.get("TargetMode") != "External" and link.get("Type", "").endswith("/worksheet")
            and re.fullmatch(r"worksheets/[A-Za-z0-9_.-]+\.xml", target)
        ):
            targets[link.get("Id")] = "xl/" + target

    result = {}
    for sheet in book.findall("{*}sheets/{*}sheet"):
        if re.sub(r"[\W_]+", "", sheet.get("name", "").casefold()) == "эталонныйкейс":
            continue
        key = next((value for field, value in sheet.attrib.items() if field.endswith("}id")), None)
        if key in targets:
            result[sheet.get("name")] = targets[key]
    return WorkbookMerges(archive, result)


class WorkbookMerges:
    def __init__(self, archive, members):
        self.archive, self.members = archive, members

    def get(self, sheet, default):
        from openpyxl.utils.cell import range_boundaries

        member = self.members.get(sheet)
        if not member:
            return default
        merges = []
        with self.archive.open(member) as source:
            stack = []
            for event, node in ElementTree.iterparse(source, events=("start", "end")):
                if event == "start":
                    stack.append(node)
                    continue
                stack.pop()
                if node.tag.rsplit("}", 1)[-1] == "mergeCell":
                    label = node.get("ref", "")
                    if not re.fullmatch(r"[A-Z]{1,3}[1-9][0-9]{0,6}:[A-Z]{1,3}[1-9][0-9]{0,6}", label):
                        raise ValueError("invalid_cell_merge")
                    merges.append(range_boundaries(label))
                    if len(merges) > 5000:
                        raise ValueError("too_many_cell_merges")
                node.clear()
                if stack:
                    stack[-1].remove(node)
        return merges


def presentation_table(table):
    rows, merges = [], []
    for row_number, row in enumerate(islice(table.iterfind("{*}tr"), _MAX_ROWS), 1):
        values = []
        for column, cell in enumerate(islice(row.iterfind("{*}tc"), _MAX_COLUMNS), 1):
            if cell.get("hMerge") in {"1", "true"} or cell.get("vMerge") in {"1", "true"}:
                values.append("")
                continue
            paragraphs = [
                "".join(node.text or "" for node in p.iterfind(".//{*}t"))
                for p in cell.iterfind(".//{*}p")
            ]
            values.append(plain_text(" ".join(paragraphs), 500))
            width, height = int(cell.get("gridSpan", 1)), int(cell.get("rowSpan", 1))
            if not (1 <= width <= _MAX_COLUMNS and 1 <= height <= _MAX_ROWS):
                raise ValueError("invalid_presentation_merge")
            if width > 1 or height > 1:
                merges.append((column, row_number, column + width - 1, row_number + height - 1))
                if len(merges) > 5000:
                    raise ValueError("too_many_cell_merges")
        rows.append(values)
    return rows, merges


def _geometry(merges):
    events = defaultdict(list)
    total_cells = 0
    for c1, r1, c2, r2 in merges:
        if c2 < c1 or r2 < r1:
            raise ValueError("invalid_cell_merge")
        if c1 > _MAX_COLUMNS or r1 > _MAX_ROWS:
            continue
        c2, r2 = min(c2, _MAX_COLUMNS), min(r2, _MAX_ROWS)
        total_cells += (c2 - c1 + 1) * (r2 - r1 + 1)
        if total_cells > _MAX_COLUMNS * _MAX_ROWS:
            raise ValueError("overlapping_or_oversized_cell_merges")
        events[r1].append((c1, c2, r2))
    return events


def _band(value):
    match = _BAND.fullmatch(_display(value))
    return match[1].strip() if match else None


def _row_view(index, values, active, events):
    active[:] = [entry for entry in active if entry[2] >= index]
    actual = {c: value for c, value in enumerate(values, 1) if _display(value)}
    active.extend((left, right, end, index, actual.get(left))
                  for left, right, end in events.get(index, ()))
    expanded = actual.copy()
    vertical = {}
    for left, right, _, anchor, value in active:
        if value is None:
            continue
        for column in range(left, right + 1):
            expanded.setdefault(column, value)
        if anchor < index and left == right and left not in actual:
            vertical[left] = (value, anchor)
    return actual, expanded, vertical


def table_records(path, rows, merges, locate, *, shared=None):
    """Emit flat rows, grouped competitor columns or metrics-by-competitor grids."""
    shared = shared or {}
    events = _geometry(merges)
    active, headers, bands = [], {}, {}
    matrix = None
    category = set()
    for index, values in enumerate(islice(rows, _MAX_ROWS), 1):
        actual, expanded, vertical = _row_view(index, values, active, events)
        if not actual:
            continue
        new_bands = {col: name for col, value in expanded.items() if (name := _band(value))}
        following_category = {col for col, value in expanded.items()
                              if _display(value).casefold() in {"конкуренты", "competitors"}}
        for col in category & actual.keys():
            name = _display(actual[col])
            if name.casefold() not in _FIELDS and not _band(name):
                new_bands.setdefault(col, name)
        category = following_category
        recognized = {col: text for col, value in actual.items()
                      if (text := _display(value)).casefold() in _FIELDS}
        metric_headers = [col for col, label in recognized.items() if _FIELDS[label.casefold()] == "metric"]
        if new_bands:
            bands = new_bands
            column = metric_headers[0] if len(metric_headers) == 1 else None
            matrix = (column, index) if (
                column is not None and set(recognized) == {column}
                and len(set(bands.values())) == len(bands)
            ) else None
            headers = recognized
            continue
        if category:
            continue
        if (len(recognized) >= 2 or len(recognized) == len(actual)) and not matrix:
            headers = recognized
            continue
        if matrix:
            metric_col, start = matrix
            metric = _display(actual.get(metric_col))
            if not metric:
                continue
            for col, competitor in sorted(bands.items()):
                if col not in actual:
                    continue
                value = actual[col]
                label_at, _ = locate(start, col, col)
                location, provenance = locate(index, min(metric_col, col), max(metric_col, col))
                result = shared | _read_metadata(((metric, value),)) | {
                    "competitor": competitor, "metric": metric, "metric_value": _display(value),
                    "offer_group": path.name + " · " + label_at + " · " + competitor,
                }
                yield _record(path, location, f"{competitor} · {metric}: {_display(value)}", result, **provenance)
            continue

        values = actual | {col: value for col, (value, _) in vertical.items() if col in headers}
        separate = defaultdict(list)
        for col in sorted(values):
            if col in bands:
                separate[bands[col]].append(col)
        common = [col for col in sorted(values) if col not in bands]
        groups = [(name, common + cols) for name, cols in separate.items()] or [(None, common)]
        for name, cols in groups:
            if not cols:
                continue
            cols = sorted(cols)
            metadata = shared | _read_metadata((headers.get(col, ""), values[col]) for col in cols)
            if name:
                metadata["competitor"] = name
            first, last = min(cols), max(cols)
            locator, provenance = locate(index, first, last)
            if name:
                locator += " · " + name
            if any(col in vertical for col in cols):
                provenance["merged_cells"] = [
                    locate(vertical[col][1], col, col)[0] for col in cols if col in vertical
                ]
            text = " | ".join(f"{headers.get(col, str(col))}: {_display(values[col])}" for col in cols)
            yield _record(path, locator, text, metadata, **provenance)
