"""Run an offline compatibility check against an exact published AI Core tree."""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
ADAPTER = (
    "pm/web_search.py", "pm/search_security.py", "pm/competitor_sources.py", "pm/yandex_search",
    "scripts/search_smoke.py", "scripts/search_monitor.py", "scripts/search_import_competitors.py",
    "scripts/search_investigation_smoke.py", "scripts/search_pim_import.py", "scripts/search_release_smoke.py",
    "tests/fixtures/yandex", "tests/search_ai_core_contract.py",
)


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--ai-ref", default="origin/feature/ai-core", help="Locally fetched AI Core ref or SHA")
    args = cli.parse_args()
    sha = subprocess.check_output(
        ["git", "rev-parse", "--verify", "--end-of-options", args.ai_ref + "^{commit}"], cwd=ROOT, text=True,
    ).strip()
    with tempfile.TemporaryDirectory(prefix="search-ai-core-") as temporary:
        folder = Path(temporary)
        checkout = folder / "ai-core"
        archive = folder / "ai-core.zip"
        subprocess.run(["git", "archive", "--format=zip", f"--output={archive}", sha], cwd=ROOT, check=True)
        with ZipFile(archive) as tree:
            tree.extractall(checkout)
        files = [*ADAPTER, *(str(path.relative_to(ROOT)) for path in sorted(ROOT.glob("tests/test_yandex*.py")))]
        for relative in files:
            source, target = ROOT / relative, checkout / relative
            if target.exists():
                cli.error(f"AI Core already owns {relative}. Review the integration manually.")
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir():
                shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__"))
            else:
                shutil.copyfile(source, target)
        print(f"Integration base {sha}. Adapter from the checked-out Search worktree.", flush=True)
        env = {key: value for key, value in os.environ.items()
               if key.upper() in {"SYSTEMROOT", "TEMP", "TMP", "TMPDIR", "PATH", "LANG", "WINDIR"}}
        env.update(PYTHONUTF8="1", PM_PRIVATE_DIR=str(folder / "state"))
        for pattern in ("test*.py", "search_ai_core_contract.py"):
            subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", pattern],
                           cwd=checkout, env=env, check=True)
        subprocess.run([sys.executable, "scripts/search_release_smoke.py"], cwd=checkout, env=env, check=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
