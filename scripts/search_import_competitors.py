"""Prepare a local JSON corpus for PM_EVIDENCE."""

import argparse
import json
import os
import sys
from pathlib import Path
from tempfile import NamedTemporaryFile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pm.competitor_sources import load_competitor_evidence


def private_json_target(path):
    target = path.resolve()
    if target.suffix.lower() != ".json" or any(
        parent == ROOT or (parent / ".git").exists() for parent in target.parents
    ):
        raise ValueError("output_must_be_json_outside_git")
    if path.is_symlink() or target.exists():
        raise ValueError("output_already_exists")
    if not target.parent.is_dir():
        raise ValueError("output_directory_missing")
    return target


def write_private_json(path, value):
    """Publish a new JSON file outside Git, with a same-directory atomic link."""
    target = private_json_target(path)
    temporary = None
    try:
        with NamedTemporaryFile(
            dir=target.parent, suffix=".tmp", mode="w", encoding="utf-8", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, allow_nan=False, indent=2)
            stream.write("\n")
        os.link(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description="Локальный импорт исследования конкурентов")
    parser.add_argument("source", type=Path, help="Исследование в формате XLSX или PPTX")
    parser.add_argument("--output", type=Path, required=True, help="Новый JSON вне репозиториев")
    parser.add_argument("--limit", type=int, default=200, help="От 1 до 2000 записей")
    args = parser.parse_args()
    try:
        items = load_competitor_evidence(args.source, limit=args.limit)
        write_private_json(args.output, items)
    except (ValueError, OSError, ImportError) as error:
        parser.exit(2, f"Импорт не выполнен ({type(error).__name__}). Проверьте формат и пути.\n")
    print(f"Подготовлено записей: {len(items)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
