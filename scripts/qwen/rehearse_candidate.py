"""Export the pinned team components to an untracked acceptance directory."""

import argparse
import io
import json
from pathlib import Path
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parents[2]
SEARCH = "813e47c6147d202fc01043998f990309a5fbfb6a"
DEPLOYMENT = "a57b681c64e61243c3e7e63ebdf863a2bf6aa4d0"
SEARCH_MODULES = {"pm/competitor_sources.py", "pm/search_security.py", "pm/web_search.py"}
SEARCH_PREFIXES = ("pm/yandex_search/", "scripts/search_", "tests/test_yandex_", "tests/fixtures/yandex/")
DEPLOYMENT_PREFIXES = ("deploy/", "scripts/deploy/", "tests/test_deployment_", "tests/deployment_")
PATCHES = (
    "0001-chat-wiring.patch", "0002-qualitative-synthesis.patch", "0003-core-evidence.patch",
    "deployment/0001-qwen-proxy.patch",
)


def git(*command):
    return subprocess.check_output(["git", "-C", ROOT, *command])


def revision(ref):
    return git("rev-parse", "--verify", "--end-of-options", ref + "^{commit}").decode().strip()


def files(ref):
    return git("ls-tree", "-rz", "--name-only", ref).decode().rstrip("\0").split("\0")


def export(out, ref, paths=()):
    if paths:
        data = git("archive", "--format=tar", ref, "--", *paths)
    else:
        data = git("archive", "--format=tar", ref)
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        archive.extractall(out, filter="data")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="Existing empty directory outside the repository")
    parser.add_argument("--qwen", default="HEAD")
    parser.add_argument("--search", default=SEARCH)
    parser.add_argument("--deployment", default=DEPLOYMENT)
    args = parser.parse_args()
    out = args.output.resolve()
    if not out.is_dir() or out.is_relative_to(ROOT) or any(out.iterdir()):
        parser.error("Use an existing empty directory outside the repository")
    qwen, search, deploy = map(revision, (args.qwen, args.search, args.deployment))
    search_paths = [name for name in files(search) if name in SEARCH_MODULES or name.startswith(SEARCH_PREFIXES)]
    deployment_paths = [name for name in files(deploy) if name == "scripts/pim_backup.py"
                        or name.startswith(DEPLOYMENT_PREFIXES)]
    if not (SEARCH_MODULES <= set(search_paths) and "deploy/nginx/paradiigma.conf.in" in deployment_paths):
        parser.error("The selected revisions do not contain the required Search and Deployment components")
    if set(search_paths) & set(files(qwen)):
        parser.error("Search overlay would replace paths already present in the Qwen revision")
    export(out, qwen)
    export(out, search, search_paths)
    export(out, deploy, deployment_paths)
    patches = [str(out / "llm_lab/patches" / name) for name in PATCHES]
    subprocess.run(["git", "apply", "--check", *patches], cwd=out, check=True)
    subprocess.run(["git", "apply", *patches], cwd=out, check=True)
    print(json.dumps({"qwen": qwen, "search": search, "deployment": deploy, "output": str(out)}, indent=2))


if __name__ == "__main__":
    main()
