"""Export the pinned team components to an untracked acceptance directory."""

import argparse
import io
import json
from pathlib import Path
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parents[2]
CORE_BASE = "f8b93b1508fdc2b3242976187c6966583a8fa03f"
CORE = "b71424d71468ccbdd9a2ceb7b44799fc07abe334"
SEARCH = "93de5460148982747093bfe50e34dbc57abbb88c"
DEPLOYMENT = "6c18b37b41d64fc041506d450ea2558b6b92e5f5"
DEPLOYMENT_BACKUP = "4f26abaeedd98c18240ce3d618562455c4595058"
SEARCH_MODULES = {"pm/competitor_sources.py", "pm/search_security.py", "pm/web_search.py"}
SEARCH_CHECK = "tests/search_ai_core_contract.py"
SEARCH_PREFIXES = ("pm/yandex_search/", "scripts/search_", "tests/test_yandex_", "tests/fixtures/yandex/")
DEPLOYMENT_PREFIXES = ("deploy/", "scripts/deploy/", "tests/test_deployment_", "tests/deployment_")
PATCHES = (
    "0001-chat-wiring.patch", "0002-qualitative-synthesis.patch", "0003-core-evidence.patch",
    "deployment/0001-qwen-proxy.patch", "deployment/0002-pim-backup-compat.patch",
)


def git(*command):
    return subprocess.check_output(["git", "-C", ROOT, *command])


def revision(ref):
    return git("rev-parse", "--verify", "--end-of-options", ref + "^{commit}").decode().strip()


def files(ref):
    return git("ls-tree", "-rz", "--name-only", ref).decode().rstrip("\0").split("\0")


def changed(ref, *, kinds=None):
    flags = [f"--diff-filter={kinds}"] if kinds else []
    data = git("diff", "--name-only", "-z", *flags, CORE_BASE, ref, "--").decode()
    return set(data.rstrip("\0").split("\0")) if data else set()


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
    parser.add_argument("--core", default=CORE)
    parser.add_argument("--search", default=SEARCH)
    parser.add_argument("--deployment", default=DEPLOYMENT)
    args = parser.parse_args()
    out = args.output.resolve()
    if not out.is_dir() or out.is_relative_to(ROOT) or any(out.iterdir()):
        parser.error("Use an existing empty directory outside the repository")
    qwen, core, search, deploy = map(revision, (args.qwen, args.core, args.search, args.deployment))
    for ref in (qwen, core):
        if subprocess.run(["git", "-C", ROOT, "merge-base", "--is-ancestor", CORE_BASE, ref]).returncode != 0:
            parser.error("Qwen and AI Core must descend from the pinned common base")
    core_paths = changed(core, kinds="ACM")
    if changed(core) != core_paths or core_paths & changed(qwen):
        parser.error("AI Core changes overlap the Qwen branch or require a reviewed deletion")
    search_paths = [name for name in files(search)
                    if name in SEARCH_MODULES or name == SEARCH_CHECK or name.startswith(SEARCH_PREFIXES)]
    deployment_paths = [name for name in files(deploy) if name == "scripts/pim_backup.py"
                        or name.startswith(DEPLOYMENT_PREFIXES)]
    if not (SEARCH_MODULES <= set(search_paths) and "deploy/nginx/paradiigma.conf.in" in deployment_paths):
        parser.error("The selected revisions do not contain the required Search and Deployment components")
    if set(search_paths) & set(files(qwen)):
        parser.error("Search overlay would replace paths already present in the Qwen revision")
    export(out, qwen)
    if core_paths:
        export(out, core, sorted(core_paths))
    export(out, search, search_paths)
    export(out, deploy, deployment_paths)
    patches = [str(out / "llm_lab/patches" / name) for name in PATCHES]
    upstream_backup = subprocess.run(["git", "-C", ROOT, "merge-base", "--is-ancestor", DEPLOYMENT_BACKUP, deploy],
                                     check=False, capture_output=True)
    # Deployment gained the marker fields and retention command at this revision.
    apply = ["git", "apply"]
    if upstream_backup.returncode == 0:
        apply.append("--exclude=scripts/pim_backup.py")
    subprocess.run([*apply, "--check", *patches], cwd=out, check=True)
    subprocess.run([*apply, *patches], cwd=out, check=True)
    print(json.dumps({"qwen": qwen, "core": core, "search": search, "deployment": deploy, "output": str(out)}, indent=2))


if __name__ == "__main__":
    main()
