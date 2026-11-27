"""Check the Git index and optionally unpublished commits without printing matches."""
import argparse
import re
import subprocess
import sys
from pathlib import PurePosixPath

PRIVATE_NAME = re.compile(r"(?:^|/)(?:private|credentials|secrets|backups|\.ssh|\.aws)(?:/|$)|"
                         r"(?:^|/)\.env(?:$|\.(?!example$).+)|"
                         r"\.(?:db|sqlite3?|pem|key|p12|pfx|gguf|zip|tar(?:\.gz)?|tgz|rar|7z|"
                         r"xlsx|xlsm|docx|pptx|pdf|log)(?:$|-(?:wal|shm|journal)$)", re.I)
TOKEN = re.compile(rb"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----|"
                   rb"\b(?:gh[pousr]_[a-zA-Z0-9]{30,}|github_pat_[a-zA-Z0-9_]{40,}|"
                   rb"sk-(?:proj-)?[a-zA-Z0-9_-]{32,}|xox[baprs]-[a-zA-Z0-9-]{20,}|"
                   rb"(?:AKIA|ASIA)[A-Z0-9]{16}|y0_[A-Za-z0-9_-]{32,}|AQVN[A-Za-z0-9_-]{32,})\b|"
                   rb"(?im:^[ \t]*(?:export[ \t]+)?(?:METRIKA_TOKEN|YANDEX_SEARCH_API_KEY|AWS_SECRET_ACCESS_KEY)"
                   rb"[ \t]*=[ \t]*[\"']?[A-Za-z0-9_./+=-]{16,})")


def git(*args):
    return subprocess.check_output(["git", *args])


def entries(since):
    for entry in filter(None, git("ls-files", "--stage", "-z").split(b"\0")):
        meta, raw = entry.split(b"\t", 1)
        mode, oid, stage = meta.split()
        yield mode, oid, raw, stage == b"0"
    if since:
        base = git("rev-parse", "--verify", "--end-of-options", since + "^{commit}").decode().strip()
        for commit in git("rev-list", f"{base}..HEAD").decode().splitlines():
            for entry in filter(None, git("ls-tree", "-r", "-z", commit).split(b"\0")):
                meta, raw = entry.split(b"\t", 1)
                mode, _, oid = meta.split()
                yield mode, oid, raw, True


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--since", help="Also scan all commits reachable from HEAD after this SHA or ref.")
    args = cli.parse_args()
    bad = False
    scanned = set()
    for mode, oid, raw, merged in entries(args.since):
        entry = (mode, oid, raw, merged)
        if entry in scanned:
            continue
        scanned.add(entry)
        path = str(PurePosixPath(raw.decode("utf8", errors="replace")))
        if TOKEN.search(raw):
            path, issue = "[redacted path]", "credential pattern in filename"
        elif not merged or mode not in (b"100644", b"100755"):
            issue = "unmerged or non-regular entry"
        elif PRIVATE_NAME.search(path):
            issue = "private artifact filename"
        elif int(git("cat-file", "-s", oid)) > 5_000_000:
            issue = "oversized artifact needs review"
        elif TOKEN.search(git("cat-file", "blob", oid)):
            issue = "credential pattern"
        else:
            continue
        print(f"{ascii(path)}: {issue}", file=sys.stderr)
        bad = True
    print("publication scan: " + ("failed" if bad else "passed"))
    return int(bad)


if __name__ == "__main__":
    raise SystemExit(main())
