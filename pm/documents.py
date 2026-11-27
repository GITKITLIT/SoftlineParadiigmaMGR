"""Read selected local documents without fetching links, macros or attachments."""
import re
from itertools import islice
from pathlib import Path
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

from .research import reference_is_excluded

MAX_FILE = 16 * 1024 * 1024
MAX_XML = 2 * 1024 * 1024
MAX_ITEMS = 160
TYPES = {".txt", ".docx", ".pptx", ".pdf"}


def snippets(path):
    path = Path(path)
    suffix = path.suffix.casefold()
    if suffix not in TYPES:
        raise ValueError("Поддерживаются TXT, DOCX, PPTX, PDF и явно выбранные диапазоны Excel")
    if path.stat().st_size > MAX_FILE:
        raise ValueError("Файл больше 16 МБ")
    if reference_is_excluded(path.stem):
        raise ValueError("Эталонный материал исключён из индекса")
    if suffix == ".txt":
        parts = (({"paragraph": n}, text) for n, text in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1))
    elif suffix == ".pdf":
        parts = pdf_pages(path)
    else:
        parts = office_text(path, suffix == ".pptx")
    result = []
    for locator, text in parts:
        normalized = " ".join(text.split())
        if not normalized:
            continue
        chunks = (normalized[i:i + 850] for i in range(0, min(len(normalized), 8500), 850))
        for index, fragment in enumerate(chunks, 1):
            result.append((locator | {"part": index}, fragment))
            if len(result) >= MAX_ITEMS:
                return result
    if not result:
        raise ValueError("Текст не найден. PDF со сканами требует отдельного OCR")
    return result


def pdf_pages(path):
    try:
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError
    except ImportError as error:
        raise ValueError("Для PDF установите зависимости из requirements.txt") from error
    with path.open("rb") as source:
        try:
            pdf = PdfReader(source)
            if pdf.is_encrypted:
                raise ValueError("Зашифрованный PDF пропущен")
            for i, page in enumerate(islice(pdf.pages, 40), 1):
                yield {"page": i}, page.extract_text() or ""
        except PdfReadError as error:
            raise ValueError("Не удалось прочитать PDF") from error


def office_text(path, slides):
    try:
        with ZipFile(path) as archive:
            if slides:
                members = [item for item in archive.infolist()
                           if re.fullmatch(r"ppt/slides/slide[0-9]{1,4}[.]xml", item.filename)]
                members.sort(key=lambda item: int(Path(item.filename).stem[5:]))
            else:
                members = [archive.getinfo("word/document.xml")]
            for member in members[:60]:
                if member.file_size > MAX_XML:
                    raise ValueError("Текстовый раздел превышает ограничение импорта")
                xml = ElementTree.fromstring(archive.read(member))
                paragraphs = [element for element in xml.iter() if element.tag.rsplit("}", 1)[-1] == "p"]
                for i, paragraph in enumerate(paragraphs, 1):
                    text = " ".join(element.text or "" for element in paragraph.iter()
                                    if element.tag.rsplit("}", 1)[-1] == "t")
                    locator = {"paragraph": i}
                    if slides:
                        locator["slide"] = int(Path(member.filename).stem[5:])
                    yield locator, text
    except (BadZipFile, KeyError, RuntimeError, ElementTree.ParseError) as error:
        raise ValueError("Не удалось прочитать текст из документа") from error
