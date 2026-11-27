"""Prepare a local JSON corpus for PM_EVIDENCE."""

import argparse
import json
import os
import sys
from pathlib import Path
from tempfile import NamedTemporaryFile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pm.competitor_sources import load_competitor_evidence, merge_competitor_corpus


def private_target(path, suffix):
    target = path.resolve()
    if target.suffix.lower() != suffix or any(
        parent == ROOT or (parent / ".git").exists() for parent in target.parents
    ):
        raise ValueError("output_must_be_outside_git")
    if path.is_symlink() or target.exists():
        raise ValueError("output_already_exists")
    if not target.parent.is_dir():
        raise ValueError("output_directory_missing")
    return target


def private_json_target(path):
    return private_target(path, ".json")


def write_private_json(path, value):
    """Publish a new JSON file outside Git, with a same-directory atomic link."""
    content = json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
    write_private_text(path, content, suffix=".json")


def write_private_text(path, text, *, suffix):
    target = private_target(path, suffix)
    temporary = None
    try:
        with NamedTemporaryFile(
            dir=target.parent, suffix=".tmp", mode="w", encoding="utf-8", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(text)
        os.link(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description="Локальный импорт исследования конкурентов")
    parser.add_argument("source", type=Path, help="Исследование в формате XLSX или PPTX")
    parser.add_argument("--output", type=Path, required=True, help="Новый JSON вне репозиториев")
    parser.add_argument("--limit", type=int, default=200, help="От 1 до 2000 записей")
    parser.add_argument("--corpus", type=Path, help="Существующий корпус PM_EVIDENCE для дополнения")
    args = parser.parse_args()
    try:
        items = load_competitor_evidence(args.source, limit=args.limit)
        if args.corpus:
            with args.corpus.open("rb") as stream:
                data = stream.read(32 * 1024 * 1024 + 1)
            if len(data) > 32 * 1024 * 1024:
                raise ValueError("evidence_corpus_too_large")
            items = merge_competitor_corpus(json.loads(data.decode("utf-8-sig")), items)
        write_private_json(args.output, items)
    except (ValueError, OSError, ImportError, RecursionError) as error:
        parser.exit(2, f"Импорт не выполнен ({type(error).__name__}). Проверьте формат и пути.\n")
    print(f"Подготовлено записей: {len(items)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
